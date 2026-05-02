"""Walk a staged install tree and classify every file."""

import json
from pathlib import Path
from typing import Any

from deb.classifier.elf_inspect import has_soname, is_elf
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json

# Sentinel return value used to signal that the file should be dropped
# entirely (not packaged).  Callers must filter entries with this kind.
_KIND_DROP = "__drop__"

_INVENTORY_FILE = "install-inventory.json"
_STAGE_RESULT_FILE = "stage-result.json"

# Directories at usr/lib/<name>/ that are NOT app-private roots.
# Checked only when parts[2] has no "-" (i.e. not a multiarch triplet).
_NON_APP_LIB_DIRS: frozenset[str] = frozenset({
    "pkgconfig",
    "debug",
    "locale",
    "gconv",
    "cmake",
    "systemd",
})
# Prefix-based exclusions for parts[2] (no-triplet case) that cannot be
# expressed as a single exact name.
_NON_APP_LIB_PREFIXES: tuple[str, ...] = ("python", "perl")

# Asset file suffixes that are unambiguously app-private resources.
# Conservative set: excludes .xml / .json / .ini for now.
_APP_ASSET_SUFFIXES: frozenset[str] = frozenset({
    ".edj", ".png", ".jpg", ".jpeg", ".svg",
    ".ttf", ".otf", ".kbd", ".dic", ".wav", ".ogg",
    ".txt", ".cfg",
})

# Systemd unit file suffixes that indicate service-integration files.
_SERVICE_SUFFIXES: frozenset[str] = frozenset({
    ".service", ".socket", ".target", ".timer",
    ".mount", ".path", ".slice", ".scope",
})


def _is_app_private_lib_path(rel: Path) -> bool:
    """Return True when *rel* is under an app-private library root.

    Recognised roots:
      usr/lib/<multiarch-triplet>/<app>/...   (triplet: contains "-")
      usr/lib/<app>/...                       (direct, non-excluded name)
      usr/libexec/<app>/...

    Returns False for well-known non-app lib locations (pkgconfig, debug,
    locale, gconv, cmake, systemd, python*, perl*).
    """
    parts = rel.parts
    if len(parts) < 3:
        return False
    if parts[0] != "usr":
        return False

    if parts[1] == "libexec":
        # usr/libexec/<app>/... — app dir is parts[2], content at parts[3]+.
        return len(parts) >= 4

    if parts[1] != "lib":
        return False

    candidate = parts[2]

    if "-" in candidate:
        # Looks like a multiarch triplet (e.g. x86_64-linux-gnu).
        # The app dir is parts[3]; content must be at parts[4]+.
        return len(parts) >= 5

    # Not a triplet — parts[2] is the app dir itself.
    # Content must be at parts[3]+, i.e. the file is *inside* the app dir.
    if candidate in _NON_APP_LIB_DIRS:
        return False
    if any(candidate.startswith(p) for p in _NON_APP_LIB_PREFIXES):
        return False
    return len(parts) >= 4


# pylint: disable=too-many-return-statements,too-many-branches
def _classify(rel: Path, abs_path: Path) -> str:
    """Return the kind string for *rel* (a path relative to the stage root).

    Classification is based exclusively on file characteristics and standard
    filesystem structure.  No project-specific paths or names are referenced.

    Conservative rule: when classification is uncertain, the file is assigned
    to 'other' which maps to the runtime package.

    Returns '_KIND_DROP' for files that must not be packaged (.la files).
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

    # --- Shared libraries (versioned — early exit) ---------------------------

    # Versioned shared library: libfoo.so.1 or libfoo.so.1.2.3
    # Detected by the presence of ".so." anywhere in the filename.
    if ".so." in s:
        return "shared_lib"

    # --- Session / desktop launcher metadata (before generic usr/share) ------

    if (len(parts) >= 3
            and parts[0] == "usr" and parts[1] == "share"
            and rel.suffix == ".desktop"):
        if parts[2] == "applications":
            return "desktop-launcher"
        if parts[2] in ("xsessions", "wayland-sessions"):
            return "session-metadata"

    # --- Generic usr/share data ----------------------------------------------

    if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "share":
        if len(parts) >= 3 and parts[2] == "doc":
            return "doc"
        if len(parts) >= 3 and parts[2] == "man":
            return "manpage"
        return "data"

    # --- Config files --------------------------------------------------------
    if parts and parts[0] == "etc":
        return "conffile"

    # --- Public executables --------------------------------------------------
    # usr/bin and usr/sbin are always public.
    if len(parts) >= 2 and parts[0] == "usr" and parts[1] in ("bin", "sbin"):
        return "binary"
    # usr/libexec/<name> at exactly depth 3 is a public helper.
    # Deeper paths (usr/libexec/<app>/...) are handled by app-private rules.
    if len(parts) == 3 and parts[0] == "usr" and parts[1] == "libexec":
        return "binary"

    # --- Systemd service-integration files -----------------------------------
    # Must run before app-private lib rules so that systemd unit files
    # under usr/lib/systemd/ or usr/lib/<triplet>/systemd/ are not
    # misidentified as app-private content.
    if rel.suffix in _SERVICE_SUFFIXES:
        if len(parts) >= 2 and parts[0] == "usr" and parts[1] == "lib":
            if "systemd" in parts:
                return "service-integration"

    # --- App-private extension cluster rules ---------------------------------

    if _is_app_private_lib_path(rel):
        # 1. Extension metadata: .desktop inside an app-private subtree.
        if rel.suffix == ".desktop":
            return "app-ext-metadata"

        # 2. Plugin shared object: .so, not a symlink, no SONAME.
        if rel.suffix == ".so" and not abs_path.is_symlink():
            if not has_soname(abs_path):
                return "app-plugin"
            return "shared_lib"

        # 3. App-private asset files.
        if rel.suffix in _APP_ASSET_SUFFIXES:
            return "app-ext-asset"

        # 4. App-private helper executable: no extension, not a symlink, ELF.
        if rel.suffix == "" and not abs_path.is_symlink() and is_elf(abs_path):
            return "app-helper"

    # --- Unversioned .so fallback --------------------------------------------
    if rel.suffix == ".so":
        if abs_path.is_symlink():
            target = abs_path.parent / abs_path.readlink()
            if ".so." in str(target.name):
                return "dev_lib"
        if has_soname(abs_path):
            return "shared_lib"
        return "other"

    # --- Conservative default ------------------------------------------------
    # All remaining files are assigned to "other" which maps to the runtime
    # package bucket.
    return "other"


def _walk_stage(stage_dir: Path) -> list[dict[str, Any]]:
    """Return a sorted list of entry dicts for every file/symlink in *stage_dir*.

    Files classified as '_KIND_DROP' (e.g. .la files) are silently omitted
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
        import stat as stat_mod
        import pwd, grp

        stat_result = abs_path.stat()
        mode = stat_result.st_mode
        is_special = bool(mode & (stat_mod.S_ISUID | stat_mod.S_ISGID | stat_mod.S_ISVTX))

        try:
            owner = pwd.getpwuid(stat_result.st_uid).pw_name
        except KeyError:
            owner = str(stat_result.st_uid)
            
        try:
            group = grp.getgrgid(stat_result.st_gid).gr_name
        except KeyError:
            group = str(stat_result.st_gid)

        entries.append({
            "is_symlink": abs_path.is_symlink(),
            "kind": kind,
            "path": "/" + str(rel),
            "mode_octal": oct(mode & 0o7777),
            "owner": owner,
            "group": group,
            "is_special": is_special,
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
