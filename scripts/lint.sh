#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
  echo "Error: not inside a git repository." >&2
  exit 1
fi

cd "$repo_root"

echo "========================================"
echo "Running lint checks"
echo "========================================"

echo ""
echo "==> YAPF (format check)"
python3 -m yapf --style google --diff --recursive packager

echo ""
echo "==> Flake8 (style + errors)"
python3 -m flake8 packager

echo ""
echo "==> Pylint (static analysis)"
python3 -m pylint packager

echo ""
echo "==> Compile check (syntax validation)"
python3 -m compileall packager

echo ""
echo "========================================"
echo "All lint checks passed cleanly ✔"
echo "========================================"