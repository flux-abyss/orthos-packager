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
mapfile -t FILES < <(find debcraft -name "*.py" | sort)
if (( ${#FILES[@]} > 0 )); then
  for file in "${FILES[@]}"; do
    set +e
    DIFF_OUTPUT="$(python3 -m yapf --style google --diff "$file" 2>&1)"
    status=$?
    set -e

    if (( status != 0 )); then
      echo "$DIFF_OUTPUT" >&2
      echo "Error: YAPF failed on $file" >&2
      exit 1
    fi

    if [[ -n "$DIFF_OUTPUT" ]]; then
      echo "$DIFF_OUTPUT"
      echo "Error: formatting issues found in $file. Run bash scripts/lint-fix.sh" >&2
      exit 1
    fi
  done
fi

echo ""
echo "==> Flake8 (style + errors)"
python3 -m flake8 debcraft

echo ""
echo "==> Pylint (static analysis)"
python3 -m pylint debcraft

echo ""
echo "==> Mypy (type checking)"
python3 -m mypy

echo ""
echo "==> Compile check (syntax validation)"
python3 -m compileall debcraft

echo ""
echo "========================================"
echo "All lint checks passed cleanly ✔"
echo "========================================"