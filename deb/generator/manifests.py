"""Install manifest helpers for orthos generator."""

from pathlib import Path
from typing import Any

# Directory prefixes under which the grouping algorithm may emit a wildcard
# install entry, subject to the exclusivity check in _coalesce_to_dirs.
# Paths not matching any prefix are always kept at file granularity.
_COLLAPSIBLE_PREFIXES = (
    # Executable install areas.
    "usr/bin",
    "usr/sbin",
    "usr/libexec",
    # Library install areas (covers multiarch triplet subdirs and app-private
    # plugin dirs such as usr/lib/x86_64-linux-gnu/<app>/).
    "usr/lib",
    # Shared data install areas (broad collapse - any exclusively-owned subdir).
    "usr/share",
    # System config area.
    "etc",
)


def _coalesce_to_dirs(
    files: list[str],
    app_name: str,
    all_staged: frozenset[str] | None = None,
) -> list[str]:
    """Group install entries into directory wildcards where exclusively owned."""
    app_prefix = f"usr/share/{app_name}"
    safe_prefixes = _COLLAPSIBLE_PREFIXES + (app_prefix,)

    staged_by_dir: dict[str, set[str]] = {}
    for f in (all_staged if all_staged is not None else files):
        parent = str(Path(f.lstrip("/")).parent)
        staged_by_dir.setdefault(parent, set()).add(f)

    pkg_files = set(files)
    result: list[str] = []
    emitted_dirs: set[str] = set()

    for f in files:
        rel = f.lstrip("/")
        parent = str(Path(rel).parent)

        if parent in emitted_dirs:
            continue

        if not any(parent == p or parent.startswith(p + "/")
                   for p in safe_prefixes):
            result.append(f)
            continue

        if staged_by_dir.get(parent, set()).issubset(pkg_files):
            result.append(parent + "/*")
            emitted_dirs.add(parent)
        else:
            result.append(f)

    return result


def _gen_install(files: list[str]) -> str:
    """Return the text of a .install file: one path per line."""
    return "\n".join(files) + "\n" if files else ""


def _check_duplicate_ownership(
    install_manifests: dict[str, list[str]],
) -> None:
    """Abort if any install path is claimed by more than one package.

    Builds a mapping of *install glob/path* -> list[package names] and raises
    'RuntimeError' if any entry is owned by more than one package.  This
    prevents overlapping file ownership in the generated .deb packages.

    Args:
        install_manifests: Mapping of package name to its list of install
            entries (paths or globs as produced by '_coalesce_to_dirs').

    Raises:
        RuntimeError: When one or more install entries appear in multiple
            packages.  The error message lists every conflicting entry.
    """
    ownership: dict[str, list[str]] = {}  # entry -> [pkg, ...]
    for pkg_name, entries in install_manifests.items():
        for entry in entries:
            ownership.setdefault(entry, []).append(pkg_name)

    duplicates = {
        entry: pkgs
        for entry, pkgs in ownership.items()
        if len(pkgs) > 1
    }
    if not duplicates:
        return

    lines = ["duplicate file ownership detected - aborting:"]
    for entry, pkgs in sorted(duplicates.items()):
        lines.append(f"  {entry!r} claimed by: {', '.join(sorted(pkgs))}")
    raise RuntimeError("\n".join(lines))
