#!/usr/bin/env python3
"""PreToolUse: protege secretos y el flujo de ramas. Salida 2 = bloquear (stderr → Claude).

Se miran rutas y comandos (no el contenido de las ediciones), para poder editar el código y la
documentación que mencionan esos nombres. Las cadenas vigiladas se escriben partidas para que
este propio fichero pueda editarse.
"""

import json
import re
import subprocess
import sys

SECRET_FILE = re.compile(r"secrets" r"\.toml$|\.config/personal-os/" r"secrets")
SECRET_SHELL = re.compile(r"secrets" r"\.toml|POS_" r"SECRET_")
ALLOWED_SHELL = re.compile(r"\bpos secrets (status|set)\b")


def block(reason: str) -> None:
    print(f"Bloqueado por .claude/hooks/guard.py: {reason}", file=sys.stderr)
    sys.exit(2)


def current_branch() -> str:
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return ""


def check(tool: str, inp: dict) -> None:
    # 1. Secretos
    if tool in ("Read", "Edit", "Write") and SECRET_FILE.search(inp.get("file_path", "")):
        block("acceso al fichero de secretos. Usa `uv run pos secrets status`.")
    if tool in ("Grep", "Glob") and SECRET_FILE.search(
        f"{inp.get('path', '')} {inp.get('pattern', '')}"
    ):
        block("búsqueda en el fichero de secretos.")
    if tool != "Bash":
        return
    cmd = inp.get("command", "")
    if SECRET_SHELL.search(cmd) and not ALLOWED_SHELL.search(cmd):
        block("acceso a secretos desde la shell. Usa `uv run pos secrets status`.")

    # 2. Git: nada de force-push, push a main ni commits/merges estando en main.
    if re.search(r"\bgit\s+push\b.*(\s--force\b|\s-f\b|--force-with-lease)", cmd):
        block("force-push no permitido.")
    if re.search(r"\bgit\s+push\b[^|;&]*\bmain\b", cmd):
        block("push directo a main. Los cambios llegan a main solo por PR desde dev.")
    if (
        re.search(r"\bgit\s+(commit|merge|rebase|cherry-pick)\b", cmd)
        and current_branch() == "main"
    ):
        block("estás en main. Trabaja en una rama feat/… desde dev.")


if __name__ == "__main__":
    data = json.load(sys.stdin)
    check(data.get("tool_name", ""), data.get("tool_input") or {})
    sys.exit(0)
