"""Read an install inventory and group files into package buckets."""

import json
from pathlib import Path
from typing import Any

from debcraft.utils.fs import ensure_dir, write_json

_INVENTORY_FILE = "install-inventory.json"
_PLAN_FILE = "package-plan.json"

# Canonical bucket order for output.
_BUCKET_ORDER: list[str] = [
    "runtime", "dev", "bin", "doc", "data", "plugins", "other"
]

# Maps inventory kind -> bucket name.
_KIND_TO_BUCKET: dict[str, str] = {
    "shared_lib": "runtime",
    "dev_lib": "dev",
    "header": "dev",
    "pkgconfig": "dev",
    "binary": "bin",
    "doc": "doc",
    "manpage": "doc",
    "data": "data",
    "plugin": "plugins",
    "other": "other",
}


def _orthos_dir(repo_path: Path) -> Path:
    """Mirror the layout used by earlier steps."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def _load_inventory(inventory_file: Path) -> dict[str, Any]:
    """Read and return the inventory JSON; raise FileNotFoundError if absent."""
    if not inventory_file.exists():
        raise FileNotFoundError(
            f"inventory file not found: {inventory_file}\n"
            f"Run 'orthos-packager inventory <repo>' first.")

    data: dict[str,
               Any] = json.loads(inventory_file.read_text(encoding="utf-8"))
    return data


def _group_into_buckets(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a list of bucket dicts in canonical order, all seven always present."""
    # Accumulate file paths per bucket.
    buckets: dict[str, list[str]] = {name: [] for name in _BUCKET_ORDER}

    for entry in entries:
        kind = entry.get("kind", "other")
        bucket = _KIND_TO_BUCKET.get(kind, "other")
        buckets[bucket].append(entry["path"])

    # Sort paths within each bucket and build output list.
    result: list[dict[str, Any]] = []
    for name in _BUCKET_ORDER:
        files = sorted(buckets[name])
        result.append({
            "file_count": len(files),
            "files": files,
            "name": name,
        })

    return result


def classify(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Read the inventory for *meta*, group into buckets, write package-plan.json.

    Returns (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    orthos = _orthos_dir(repo)
    inventory_file = orthos / _INVENTORY_FILE

    inventory = _load_inventory(
        inventory_file)  # raises FileNotFoundError if missing

    entries: list[dict[str, Any]] = inventory.get("entries", [])
    package_buckets = _group_into_buckets(entries)

    plan_file = orthos / _PLAN_FILE
    ensure_dir(orthos)

    result: dict[str, Any] = {
        "inventory_file": str(inventory_file),
        "package_buckets": package_buckets,
        "plan_file": str(plan_file),
        "repo_path": str(repo),
        "total_files": len(entries),
    }

    write_json(plan_file, result)
    return 0, result
