"""Run on the VPS: consume optional keys on stdin; never print their values."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

values = json.load(sys.stdin)
keys = ("YURETS_TURNSTILE_SITE_KEY", "YURETS_TURNSTILE_SECRET_KEY")
if set(values) != set(keys) or not all(isinstance(values[k], str) and values[k] for k in keys):
    raise SystemExit("Both Turnstile keys are required")
path = Path("/root/yurets/.env.plex")
text = path.read_text()
lines = [line for line in text.splitlines() if not any(line.startswith(k + "=") for k in keys)]
for key in keys:
    lines.append(key + "=" + json.dumps(values[key], ensure_ascii=False).replace("$", "$$"))
updated = "\n".join(lines) + "\n"
if updated != text:
    backup = Path("/root/.local/state/yurets-deploy")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, backup_name = tempfile.mkstemp(prefix="before-turnstile-", dir=backup)
    os.close(descriptor)
    shutil.copyfile(path, backup_name)
    descriptor, temporary = tempfile.mkstemp(prefix=".env.plex.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(updated)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
print("Turnstile observation configuration ready.")
