#!/usr/bin/env python3
"""PostToolUse (Edit/Write): formatea y revisa con ruff el fichero Python editado.
Si quedan errores, salida 2 para que Claude los vea y los corrija."""

import json
import os
import shutil
import subprocess
import sys

data = json.load(sys.stdin)
path = (data.get("tool_input") or {}).get("file_path", "")
root = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
if not path.endswith(".py") or not os.path.exists(path):
    sys.exit(0)
rel = os.path.relpath(path, root)
if rel.startswith(".."):
    sys.exit(0)

uv = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")
subprocess.run([uv, "run", "--quiet", "ruff", "format", "--quiet", path], cwd=root)
check = subprocess.run(
    [uv, "run", "--quiet", "ruff", "check", "--fix", "--quiet", "--output-format", "concise", path],
    cwd=root,
    capture_output=True,
    text=True,
)
if check.returncode != 0:
    print(f"ruff encontró problemas en {rel}:\n{check.stdout}{check.stderr}", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
