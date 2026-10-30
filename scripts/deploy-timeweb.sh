#!/usr/bin/env bash
# Executed on the existing Timeweb VPS through SSH by GitHub Actions.
set -Eeuo pipefail
umask 077

TARGET=${1:?Expected Git commit SHA}
[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid commit SHA'; exit 1; }
DEPLOY_DIR=${YURETS_DEPLOY_DIR:-/root/yurets}
STATE_DIR=${YURETS_DEPLOY_STATE_DIR:-/root/.local/state/yurets-deploy}
HEALTH_ATTEMPTS=${YURETS_DEPLOY_HEALTH_ATTEMPTS:-60}
HEALTH_INTERVAL=${YURETS_DEPLOY_HEALTH_INTERVAL:-5}
CONTAINER=yurets-backend-1
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
exec 9>"$STATE_DIR/deploy.lock"
flock -n 9 || { echo 'Another deployment is running'; exit 1; }
cd "$DEPLOY_DIR"

git fetch origin main
REMOTE=$(git rev-parse origin/main)
if [[ "$REMOTE" != "$TARGET" ]]; then
  echo 'A newer main commit exists; skipping this superseded deployment.'
  exit 75
fi
git merge-base --is-ancestor HEAD "$TARGET"
CHANGES=$(git diff --name-only HEAD)
while IFS= read -r changed; do
  [[ -z "$changed" || "$changed" == docker-compose.yml ]] || {
    echo "Local changes in $changed; deployment stopped without overwriting them."
    exit 1
  }
done <<< "$CHANGES"

# Keep the server's loopback port binding, env files, data volume and proxy trust.
# A future upstream Compose conflict fails safely during the fast-forward merge.
PREVIOUS_IMAGE=$(docker inspect --format '{{.Image}}' "$CONTAINER")
BACKUP=$(mktemp -d "$STATE_DIR/attempt.XXXXXXXX")
docker compose config --format json > "$BACKUP/compose.json"
python3 - "$BACKUP/compose.json" "$PREVIOUS_IMAGE" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
config = json.loads(path.read_text())
service = config['services']['backend']
service.pop('build', None)
service['image'] = sys.argv[2]
service['pull_policy'] = 'never'
path.write_text(json.dumps(config))
PY
for file in .env .env.plex docker-compose.yml; do
  if [[ -f "$file" ]]; then cp -p "$file" "$BACKUP/"; fi
done
# SQLite's backup API includes committed WAL data without stopping the radio.
docker exec -i "$CONTAINER" python - <<'PY'
import os, sqlite3
from pathlib import Path
source = Path(os.environ.get('YURETS_DATA_DIR', './data')) / 'radio.sqlite3'
if not source.is_file():
    raise SystemExit('Expected radio database is missing; refusing deployment')
target = '/tmp/yurets-predeploy.sqlite3'
with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as live:
    with sqlite3.connect(target) as copy:
        live.backup(copy)
os.chmod(target, 0o600)
PY
docker cp "$CONTAINER:/tmp/yurets-predeploy.sqlite3" "$BACKUP/radio.sqlite3"
docker cp "$CONTAINER:/data/listener-cookie.key" "$BACKUP/listener-cookie.key"
chmod 600 "$BACKUP"/*
docker image tag "$PREVIOUS_IMAGE" yurets-backend:previous-deploy
printf 'Recovery snapshot: %s\n' "$BACKUP"

git merge --ff-only "$TARGET"
# The old container remains live throughout the build. No down/prune/reset.
docker compose build backend

wait_for_radio() {
  for ((attempt=1; attempt<=HEALTH_ATTEMPTS; attempt++)); do
    if curl --fail --silent --max-time 5 http://127.0.0.1:8000/health > "$STATE_DIR/health.json" &&
      python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(not(d.get("status")=="ok" and d.get("source")=="plex"))' "$STATE_DIR/health.json" 2>/dev/null; then
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

SWITCHED=0
finish() {
  result=$?
  trap - EXIT
  if [[ "$result" != 0 && "$SWITCHED" == 1 ]]; then
    echo 'New release failed verification; restoring the previous container image.'
    if docker compose --project-name yurets -f "$BACKUP/compose.json" up -d --no-build --no-deps backend && wait_for_radio; then
      echo 'Previous radio restored. Source files remain at the attempted commit for diagnosis.'
    else
      echo "ROLLBACK NEEDS ATTENTION. Recovery snapshot: $BACKUP"
    fi
  fi
  exit "$result"
}
trap finish EXIT
SWITCHED=1
docker compose up -d --no-build --no-deps backend
wait_for_radio
# Check the real stream, not just a process or a listening port. Never log visitors.
docker exec -i "$CONTAINER" python - <<'PY'
import subprocess, urllib.request
for route in ('/stats', '/api/analytics', '/api/queue', '/api/schedule'):
    with urllib.request.urlopen('http://127.0.0.1:8000' + route, timeout=15) as response:
        if response.status != 200:
            raise SystemExit('HTTP verification failed')
with urllib.request.urlopen('http://127.0.0.1:8000/stream', timeout=15) as response:
    sample = response.read(65536)
if len(sample) < 65536:
    raise SystemExit('Audio stream ended prematurely')
result = subprocess.run(
    ['ffmpeg', '-v', 'error', '-i', 'pipe:0', '-t', '2', '-f', 'null', '-'],
    input=sample, capture_output=True, timeout=20,
)
if result.returncode:
    raise SystemExit('Audio decoding failed')
print('Public statistics, queue, schedule and Plex audio verified.')
PY
SWITCHED=0
printf '%s\n' "$TARGET" > "$STATE_DIR/deployed-commit"
printf 'Deployed %s successfully.\n' "$TARGET"
