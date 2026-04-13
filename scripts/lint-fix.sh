#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
  echo "Error: not inside a git repository." >&2
  exit 1
fi

cd "$repo_root"

echo "========================================"
echo "Running lint auto-fix (YAPF)"
echo "========================================"

python3 -m yapf --style google --in-place --recursive packager

echo ""
echo "========================================"
echo "Formatting applied."
echo "Now run: bash scripts/lint.sh"
echo "========================================"