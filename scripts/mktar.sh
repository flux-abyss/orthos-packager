#!/usr/bin/env bash
# dev-mktar.sh — create a source archive of the warp project
#
# Usage:
#   bash dev-mktar.sh                  # tracked files only (default)
#   bash dev-mktar.sh --include-untracked  # tracked + untracked (excluding .gitignored)
#
# The archive is always written to the repository root.
# Tarballs, .dsc files, .diff.gz files, and missing paths are always excluded.
set -euo pipefail

include_untracked=0

case "${1-}" in
  --include-untracked)
    include_untracked=1
    shift
    ;;
  "")
    ;;
  --help | -h)
    sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *)
    echo "Usage: $0 [--include-untracked]" >&2
    exit 1
    ;;
esac

if ! command -v git >/dev/null 2>&1; then
  echo "Error: git is required." >&2
  exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
  echo "Error: tar is required." >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$repo_root" ]]; then
  echo "Error: not inside a git repository." >&2
  exit 1
fi

cd "$repo_root"

repo_name="$(basename "$repo_root")"
timestamp="$(date +%Y%m%d-%H%M%S)"
archive_root="${repo_name}-${timestamp}"
out_path="${repo_root}/${archive_root}.tar.gz"

tmp_list="$(mktemp)"
tmp_filtered="$(mktemp)"
trap 'rm -f "$tmp_list" "$tmp_filtered"' EXIT

if [[ "$include_untracked" -eq 1 ]]; then
  git ls-files -z --cached --others --exclude-standard >"$tmp_list"
else
  git ls-files -z --cached >"$tmp_list"
fi

python3 - <<'PY' "$tmp_list" "$tmp_filtered" "$(basename "$out_path")"
import os
import sys

src, dst, out_name = sys.argv[1], sys.argv[2], sys.argv[3]
items = open(src, "rb").read().split(b"\0")

_SKIP_EXTS = {".tar.gz", ".tar.xz", ".diff.gz", ".dsc"}

kept = 0
skipped_missing = 0
skipped_blobs = 0

with open(dst, "wb") as f:
    for item in items:
        if not item:
            continue
        path = item.decode("utf-8", errors="surrogateescape")
        base = os.path.basename(path)

        # Skip the output archive itself
        if base == out_name:
            skipped_blobs += 1
            continue

        # Skip known binary blob types (tarballs, Debian source packages)
        if any(base.endswith(ext) for ext in _SKIP_EXTS):
            skipped_blobs += 1
            continue

        # Skip paths that no longer exist in the working tree
        # (renamed/deleted but not yet staged — git ls-files --cached is stale)
        if not os.path.lexists(path):
            skipped_missing += 1
            continue

        f.write(item + b"\0")
        kept += 1

print(f"Kept files:      {kept}", file=sys.stderr)
print(f"Skipped missing: {skipped_missing}", file=sys.stderr)
print(f"Skipped blobs:   {skipped_blobs}", file=sys.stderr)
PY

if [[ ! -s "$tmp_filtered" ]]; then
  echo "Error: no files left after filtering." >&2
  exit 1
fi

tar -czf "$out_path" \
  --null -T "$tmp_filtered" \
  --transform "s|^|${archive_root}/|"

file_count="$(
  python3 - <<'PY' "$tmp_filtered"
import sys
data = open(sys.argv[1], 'rb').read()
print(data.count(b'\0'))
PY
)"

echo "Archive created successfully."
echo "Mode:         $([[ "$include_untracked" -eq 1 ]] && echo 'tracked + untracked' || echo 'tracked only')"
echo "Files packed: $file_count"
echo "Output:       $out_path"