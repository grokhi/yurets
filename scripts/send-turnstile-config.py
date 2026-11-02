"""GitHub runner helper: secrets travel over encrypted SSH stdin, not argv."""

import json
import os
import shlex
import subprocess
from pathlib import Path

values = {
    key: os.environ.get(key, "")
    for key in ("YURETS_TURNSTILE_SITE_KEY", "YURETS_TURNSTILE_SECRET_KEY")
}
if not any(values.values()):
    print("Turnstile keys not supplied; retaining existing VPS settings.")
    raise SystemExit(0)
if not all(values.values()):
    raise SystemExit("Configure both Turnstile repository secrets")
script = Path("scripts/configure-turnstile.py").read_text()
subprocess.run(
    [
        "sshpass",
        "-e",
        "ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f'UserKnownHostsFile={Path.home() / ".ssh/known_hosts"}',
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "PreferredAuthentications=password,keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=1",
        "root@147.45.100.176",
        "python3 -c " + shlex.quote(script),
    ],
    input=json.dumps(values),
    text=True,
    check=True,
)
