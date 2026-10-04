#!/usr/bin/env bash
# Comprobaciones completas del proyecto. Las usan la CI y los hooks de Claude Code.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-uv}"
echo "▶ ruff format";  $UV run ruff format --check src tests
echo "▶ ruff check";   $UV run ruff check src tests
echo "▶ fronteras";    $UV run lint-imports
echo "▶ tests";        $UV run pytest -q
