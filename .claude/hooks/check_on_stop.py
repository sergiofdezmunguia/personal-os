#!/usr/bin/env python3
"""Stop: si hay cambios de código sin verificar, ejecuta scripts/check.sh.
Si falla, salida 2: Claude no da el trabajo por terminado y ve el error.
Solo se ejecuta cuando el contenido del árbol de trabajo cambió desde la última verificación."""

import hashlib
import json
import os
import shutil
import subprocess
import sys

data = json.load(sys.stdin)
if data.get("stop_hook_active"):
    sys.exit(0)  # evita bucles: ya se bloqueó una vez en este turno

root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
paths = ["src", "tests", "bridge", "pyproject.toml", "uv.lock", "scripts"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True).stdout


changed = git("status", "--porcelain", "--", *paths)
if not changed.strip():
    sys.exit(0)

fingerprint = hashlib.sha256((git("diff", "HEAD", "--", *paths) + changed).encode()).hexdigest()
for line in changed.splitlines():
    f = os.path.join(root, line[3:].strip())
    if line.startswith("??") and os.path.isfile(f):
        fingerprint = hashlib.sha256((fingerprint + open(f, "rb").read().hex()).encode()).hexdigest()

stamp = os.path.join(root, ".claude", ".last_check")
if os.path.exists(stamp) and open(stamp).read().strip() == fingerprint:
    sys.exit(0)

env = dict(os.environ, UV=shutil.which("uv") or os.path.expanduser("~/.local/bin/uv"))
result = subprocess.run(
    [os.path.join(root, "scripts", "check.sh")], cwd=root, capture_output=True, text=True, env=env
)
if result.returncode != 0:
    out = (result.stdout + result.stderr)[-3000:]
    print(f"scripts/check.sh falla; corrígelo antes de terminar:\n{out}", file=sys.stderr)
    sys.exit(2)
with open(stamp, "w") as fh:
    fh.write(fingerprint)
sys.exit(0)
