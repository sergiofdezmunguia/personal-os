"""Los hooks de Claude Code (.claude/hooks) son parte del sistema: se prueban como el resto."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / ".claude" / "hooks" / "guard.py"
FORMAT = ROOT / ".claude" / "hooks" / "format_python.py"

SECRETS_FILE = "/home/u/.config/personal-os/" + "secrets" + ".toml"


def run(hook: Path, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={"CLAUDE_PROJECT_DIR": str(ROOT), "PATH": "/usr/bin:/bin"},
    )


def bash(cmd: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Read", "tool_input": {"file_path": SECRETS_FILE}},
        {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": SECRETS_FILE}},
        bash("cat " + SECRETS_FILE),
        bash("echo $POS_" + "SECRET_ICLOUD_APP_PASSWORD"),
        bash("git push origin main"),
        bash("git push " + "--force origin feat/x"),
        bash("git push -f origin dev"),
    ],
)
def test_guard_blocks(payload):
    res = run(GUARD, payload)
    assert res.returncode == 2 and "Bloqueado" in res.stderr


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Edit", "tool_input": {"file_path": "src/personal_os/core/secrets.py"}},
        {"tool_name": "Read", "tool_input": {"file_path": "docs/setup/iphone.md"}},
        bash("uv run pos secrets status"),
        bash("git push -u origin dev"),
        bash("git push -u origin feat/mcp-server"),
        bash("uv run pytest -q"),
    ],
)
def test_guard_allows(payload):
    assert run(GUARD, payload).returncode == 0


def test_format_hook_reports_remaining_lint_errors(tmp_path):
    ok = run(FORMAT, {"tool_input": {"file_path": str(ROOT / "src/personal_os/core/ids.py")}})
    assert ok.returncode == 0
    outside = tmp_path / "x.py"
    outside.write_text("import os\n")
    assert (
        run(FORMAT, {"tool_input": {"file_path": str(outside)}}).returncode == 0
    )  # fuera del repo: ignora
