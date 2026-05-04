"""inventory command handler."""

from deb.inventory.install_inventory import build_inventory
from deb.utils.log import error, info


def cmd_inventory(repo_path: str, probe) -> int:
    """Inventory the staged install tree and write install-inventory.json."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        rc, result = build_inventory(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"stage:   {result['stage_dir']}")
    info(f"files:   {result['total_files']}")

    for kind, count in sorted(result["counts_by_kind"].items()):
        info(f"  {kind:<12} {count}")

    info(f"wrote:   {result['inventory_file']}")

    return rc
