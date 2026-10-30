"""Exercise the production shell workflow with fake Git/Docker/network commands."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-timeweb.sh"
TARGET = "a" * 40

SHIM = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['MOCK_ROOT'])
with (root / 'calls').open('a') as f:
    f.write(json.dumps([name, *args]) + '\n')
def fail(flag):
    return os.environ.get(flag) == '1'
if name == 'git':
    if args[:1] == ['rev-parse']:
        print(('b' if fail('SUPERSEDED') else 'a') * 40)
    elif args[:1] == ['diff']:
        print('src/settings.py' if fail('DIRTY') else 'docker-compose.yml')
    elif args[:1] == ['merge'] and fail('MERGE_CONFLICT'):
        sys.exit(1)
elif name == 'docker':
    if args[:1] == ['inspect']:
        print('sha256:previous')
    elif args[:2] == ['compose', 'config']:
        print(json.dumps({'name': 'yurets', 'services': {'backend': {
            'build': {'context': '/root/yurets'}, 'image': 'yurets-backend',
            'ports': [{'host_ip': '127.0.0.1', 'target': 8000, 'published': '8000'}],
            'environment': {'TOKEN': 'test$$token'},
            'volumes': [{'type': 'bind', 'source': '/root/yurets/data', 'target': '/data'}]
        }}}))
    elif args[:2] == ['compose', 'build'] and fail('BUILD_FAIL'):
        sys.exit(1)
    elif args[:1] == ['compose'] and 'up' in args:
        rollback = '-f' in args
        (root / 'phase').write_text('rollback' if rollback else 'new')
        if fail('ROLLBACK_FAIL') if rollback else fail('SWITCH_FAIL'):
            sys.exit(1)
    elif args[:1] == ['cp']:
        pathlib.Path(args[-1]).write_text('fake-backup')
    elif args[:1] == ['exec']:
        program = sys.stdin.read()
        if 'ffmpeg' in program and fail('VERIFY_FAIL'):
            sys.exit(1)
elif name == 'curl':
    phase = (root / 'phase').read_text()
    if phase == 'new' and fail('HEALTH_FAIL'):
        sys.exit(22)
    print(json.dumps({'status': 'ok', 'source': 'plex'}))
'''


class DeployTests(unittest.TestCase):
    def run_deploy(self, **flags):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        checkout = root / "repo"
        checkout.mkdir()
        (checkout / "docker-compose.yml").write_text('ports: ["127.0.0.1:8000:8000"]\n')
        (checkout / ".env.plex").write_text("TOKEN=fake-secret\n")
        (checkout / ".env").write_text("SETTING=value\n")
        binaries = root / "bin"
        binaries.mkdir()
        for name in ("git", "docker", "curl"):
            path = binaries / name
            path.write_text(SHIM)
            path.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{binaries}:{os.environ['PATH']}",
            MOCK_ROOT=str(root),
            YURETS_DEPLOY_DIR=str(checkout),
            YURETS_DEPLOY_STATE_DIR=str(root / "state"),
            YURETS_DEPLOY_HEALTH_ATTEMPTS="1",
            YURETS_DEPLOY_HEALTH_INTERVAL="0",
            **{key: "1" for key, value in flags.items() if value},
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), TARGET], env=env, text=True, capture_output=True, timeout=15
        )
        calls = [json.loads(line) for line in (root / "calls").read_text().splitlines()]
        return root, result, calls

    @staticmethod
    def switches(calls):
        return [c for c in calls if c[:2] == ["docker", "compose"] and "up" in c]

    def test_success_preserves_config_and_private_backups(self):
        root, result, calls = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.switches(calls)), 1)
        self.assertEqual((root / "state/deployed-commit").read_text().strip(), TARGET)
        backup = next((root / "state").glob("attempt.*"))
        config = json.loads((backup / "compose.json").read_text())
        service = config["services"]["backend"]
        self.assertEqual(service["image"], "sha256:previous")
        self.assertNotIn("build", service)
        self.assertEqual(service["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(service["environment"]["TOKEN"], "test$$token")
        self.assertTrue((backup / "radio.sqlite3").is_file())
        self.assertTrue((backup / "listener-cookie.key").is_file())
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "compose.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("fake-secret", result.stdout + result.stderr)
        self.assertIn('127.0.0.1:8000:8000', (root / 'repo/docker-compose.yml').read_text())

    def test_build_failure_leaves_running_container_alone(self):
        root, result, calls = self.run_deploy(BUILD_FAIL=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.switches(calls), [])
        self.assertFalse((root / "state/deployed-commit").exists())

    def test_health_or_audio_failure_rolls_back(self):
        for flag in ("HEALTH_FAIL", "VERIFY_FAIL", "SWITCH_FAIL"):
            with self.subTest(flag=flag):
                root, result, calls = self.run_deploy(**{flag: True})
                self.assertNotEqual(result.returncode, 0)
                switches = self.switches(calls)
                self.assertEqual(len(switches), 2)
                self.assertIn("-f", switches[1])
                self.assertIn("Previous radio restored", result.stdout)
                self.assertFalse((root / "state/deployed-commit").exists())

    def test_failed_rollback_is_reported_as_failure(self):
        _, result, _ = self.run_deploy(HEALTH_FAIL=True, ROLLBACK_FAIL=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROLLBACK NEEDS ATTENTION", result.stdout)
        self.assertNotIn("Previous radio restored", result.stdout)

    def test_local_source_edits_are_never_overwritten(self):
        _, result, calls = self.run_deploy(DIRTY=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[:2] == ["git", "merge"] for c in calls))
        self.assertFalse(any(c[0] == "docker" for c in calls))

    def test_compose_merge_conflict_does_not_stop_radio(self):
        _, result, calls = self.run_deploy(MERGE_CONFLICT=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.switches(calls), [])

    def test_superseded_commit_is_not_deployed(self):
        _, result, calls = self.run_deploy(SUPERSEDED=True)
        self.assertEqual(result.returncode, 75)
        self.assertFalse(any(c[0] == "docker" for c in calls))
