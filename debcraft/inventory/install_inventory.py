"""Walk a staged install tree and classify every file."""

import json
from pathlib import Path
from typing import Any

from debcraft.paths import orthos_dir
from debcraft.utils.fs import ensure_dir, write_json

_INVENTORY_FILE = "install-inventory.json"
_STAGE_RESULT_FILE = "stage-result.json"


# pylint: disable=too-many-return-statements
def _classify(rel: Path) -> str:
    """Return the kind string for *rel* (a path relative to the stage root)."""
    # Use string form for substring checks; part-based for segment checks.
    s = str(rel)

    # shared_lib: contains .so. (i.e. libfoo.so.1 or libfoo.so.1.2.3)
    if ".so." in s:
        return "shared_lib"

    # dev_lib: ends with exactly .so
    if rel.suffix == ".so":
        return "dev_lib"

    parts = rel.parts

    # header: under usr/include
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "include":
        return "header"

    # pkgconfig: .pc extension or inside a pkgconfig directory
    if rel.suffix == ".pc" or "pkgconfig" in parts:
        return "pkgconfig"

    # binary: under usr/bin or usr/sbin
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] in ("bin", "sbin"):
        return "binary"

    # plugin: path segment named modules or plugins
    if "modules" in parts or "plugins" in parts:
        return "plugin"

    # doc: usr/share/doc/...
    if len(parts) >= 3 and parts[0] == "usr" and parts[1] == "share" and parts[
            2] == "doc":
        return "doc"

    # manpage: usr/share/man/...
    if len(parts) >= 3 and parts[0] == "usr" and parts[1] == "share" and parts[
            2] == "man":
        return "manpage"

    # data: anywhere else under usr/share
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "share":
        return "data"

    return "other"


def _walk_stage(stage_dir: Path) -> list[dict[str, Any]]:
    """Return a sorted list of entry dicts for every file/symlink in *stage_dir*."""
    entries: list[dict[str, Any]] = []

    for abs_path in sorted(stage_dir.rglob("*")):
        if abs_path.is_dir() and not abs_path.is_symlink():
            continue  # skip plain directories

        rel = abs_path.relative_to(stage_dir)
        entries.append({
            "is_symlink": abs_path.is_symlink(),
            "kind": _classify(rel),
            "path": "/" + str(rel),
        })

    return entries


def _check_stage_success(orthos: Path) -> None:
    """Raise if the most recent stage result reports failure or is missing.

    Reads stage-result.json and checks that:
    - the file exists (stage was run)
    - success == True (stage did not fail)
    - the stage/ directory is not empty (install produced files)

    Raises FileNotFoundError or ValueError with a clear message.
    """
    result_file = orthos / _STAGE_RESULT_FILE
    if not result_file.exists():
        raise FileNotFoundError(
            f"stage-result.json not found: {result_file}\n"
            f"Run 'orthos-packager stage <repo>' first.")

    data = json.loads(result_file.read_text(encoding="utf-8"))
    if not data.get("success", False):
        step = data.get("failure_step", "unknown step")
        log = data.get("log_file", "<no log>")
        raise ValueError(
            f"stage failed at: {step}\n"
            f"Fix the build error and rerun 'orthos-packager stage'.\n"
            f"See log: {log}")

    stage_dir = orthos / "stage"
    if not stage_dir.exists() or not any(stage_dir.rglob("*")):
        raise ValueError(
            f"stage directory is empty: {stage_dir}\n"
            f"Meson install produced no files. Rerun 'orthos-packager stage'.")


def build_inventory(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Walk the staged tree for *meta* and write an inventory JSON.

    Raises FileNotFoundError or ValueError if stage has not completed
    successfully, or if the install tree is empty.
    Returns (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    orthos = orthos_dir(repo)

    # Fail closed: refuse to inventory a failed or empty stage.
    _check_stage_success(orthos)

    stage_dir = orthos / "stage"
    entries = _walk_stage(stage_dir)

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1

    inventory_file = orthos / _INVENTORY_FILE
    ensure_dir(orthos)

    result: dict[str, Any] = {
        "counts_by_kind": counts,
        "entries": entries,
        "inventory_file": str(inventory_file),
        "repo_path": str(repo),
        "stage_dir": str(stage_dir),
        "total_files": len(entries),
    }

    write_json(inventory_file, result)
    return 0, result
