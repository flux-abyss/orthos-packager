"""Walk a staged install tree and classify every file."""

import json
from pathlib import Path
from typing import Any

from deb.classifier.elf_inspect import has_soname
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json

# Sentinel return value used to signal that the file should be dropped
# entirely (not packaged).  Callers must filter entries with this kind.
_KIND_DROP = "__drop__"

_INVENTORY_FILE = "install-inventory.json"
_STAGE_RESULT_FILE = "stage-result.json"


# pylint: disable=too-many-return-statements
def _classify(rel: Path, abs_path: Path) -> str:
    """Return the kind string for *rel* (a path relative to the stage root).

    Classification is based exclusively on file characteristics and standard
    filesystem structure.  No project-specific paths or names are referenced.

    Conservative rule: when classification is uncertain, the file is assigned
    to ``other`` which maps to the runtime package.

    Returns ``_KIND_DROP`` for files that must not be packaged (.la files).
    """
    parts = rel.parts
    s = str(rel)

    # --- Unconditional drops -------------------------------------------------
    # Libtool archive files (.la) are never useful in binary packages.
    if rel.suffix == ".la":
        return _KIND_DROP

    # --- Development files (high confidence only) ----------------------------

    # Headers: /usr/include/**
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "include":
        return "header"

    # pkg-config metadata files.
    if rel.suffix == ".pc" or "pkgconfig" in parts:
        return "pkgconfig"

    # Static libraries are development artifacts.
    if rel.suffix == ".a":
        return "dev_lib"

    # --- Shared libraries ----------------------------------------------------

    # Versioned shared library: libfoo.so.1 or libfoo.so.1.2.3
    # Detected by the presence of ".so." anywhere in the filename.
    if ".so." in s:
        return "shared_lib"

    # Unversioned *.so: inspect to decide between dev symlink and plugin.
    if rel.suffix == ".so":
        if abs_path.is_symlink():
            # A symlink to a versioned .so is a -dev linker helper.
            target = abs_path.parent / abs_path.readlink()
            if ".so." in str(target.name):
                return "dev_lib"
        # Not a symlink, or points to something unversioned: treat as a
        # runtime-loaded plugin/module.  Use SONAME presence as a secondary
        # signal: a real shared lib that forgot its version suffix will have
        # a SONAME; a pure plugin typically will not.
        if has_soname(abs_path):
            # Carries a SONAME — treat as a shared library (runtime).
            return "shared_lib"
        # No SONAME → runtime-loaded object (plugin); goes to runtime as well
        # so it is never split into a separate dev package by mistake.
        return "other"

    # --- Data files ----------------------------------------------------------

    # Everything under /usr/share goes to data (includes doc, man, etc.).
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "share":
        # Distinguish documentation and man pages for finer bucket granularity
        # while keeping classification path-based and generic.
        if len(parts) >= 3 and parts[2] == "doc":
            return "doc"
        if len(parts) >= 3 and parts[2] == "man":
            return "manpage"
        return "data"

    # --- Config files --------------------------------------------------------
    if parts and parts[0] == "etc":
        return "other"  # /etc → runtime (mapped via "other" -> runtime)

    # --- Executables ---------------------------------------------------------
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] in ("bin", "sbin",
                                                               "libexec"):
        return "binary"

    # --- Conservative default ------------------------------------------------
    # All remaining files (including /usr/lib objects that are not .so)
    # are assigned to "other" which maps to the runtime package bucket.
    return "other"


def _walk_stage(stage_dir: Path) -> list[dict[str, Any]]:
    """Return a sorted list of entry dicts for every file/symlink in *stage_dir*.

    Files classified as ``_KIND_DROP`` (e.g. .la files) are silently omitted
    from the returned list and will not appear in any generated package.
    """
    entries: list[dict[str, Any]] = []

    for abs_path in sorted(stage_dir.rglob("*")):
        if abs_path.is_dir() and not abs_path.is_symlink():
            continue  # skip plain directories

        rel = abs_path.relative_to(stage_dir)
        kind = _classify(rel, abs_path)
        if kind == _KIND_DROP:
            continue  # silently drop; do not package these files
        entries.append({
            "is_symlink": abs_path.is_symlink(),
            "kind": kind,
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
