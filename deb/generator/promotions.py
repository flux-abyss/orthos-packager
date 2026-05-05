"""Post-layout install manifest promotion rules for orthos generator."""

from typing import Any

from deb.utils.log import info


def _promote_etc_to_primary(
    primary_pkg: str,
    install_manifests: dict[str, list[str]],
) -> None:
    """Move all etc/ entries from secondary packages to the primary package.

    Debian policy: a file must be owned by exactly one package.  Config
    files under /etc belong conceptually to the main runtime package
    (maintainer scripts, conffile declarations, and debconf all expect the
    primary package to own them).  When the classifier places an etc/ file
    in a secondary bucket (e.g. *-other, *-data), dpkg raises a file-overwrite
    conflict if the primary package also installs into that directory.

    This function is the authoritative enforcement point.
    It operates in-place on *install_manifests* before any .install file is
    written, so the correction is transparent to the rest of the generator.

    Rules:
      - Any entry that starts with 'etc/' or 'etc/*' is an etc/ file.
      - Such entries are removed from every secondary package manifest.
      - They are added to the primary package manifest (deduplicated).
      - The primary package manifest is re-sorted for stable output.
    """
    if primary_pkg not in install_manifests:
        return

    primary_entries: set[str] = set(install_manifests[primary_pkg])
    promoted: list[str] = []

    for pkg_name, entries in install_manifests.items():
        if pkg_name == primary_pkg:
            continue
        kept: list[str] = []
        for entry in entries:
            # Normalise: strip leading slashes before prefix check.
            rel = entry.lstrip("/")
            if rel == "etc" or rel.startswith("etc/") or rel.startswith("etc/*"):
                if entry not in primary_entries:
                    promoted.append(entry)
                    primary_entries.add(entry)
                    info(
                        f"generator: promoted {entry!r} from {pkg_name!r} "
                        f"to primary package {primary_pkg!r} (etc/ policy)"
                    )
                else:
                    info(
                        f"generator: removed {entry!r} from {pkg_name!r} "
                        f"(already in primary {primary_pkg!r}; etc/ policy)"
                    )
            else:
                kept.append(entry)
        install_manifests[pkg_name] = kept

    if promoted:
        install_manifests[primary_pkg] = sorted(primary_entries)


def _promote_app_lib_dirs_to_primary(
    app_name: str,
    primary_pkg: str,
    install_manifests: dict[str, list[str]],
) -> None:
    """Move app-private lib subtree entries from secondary packages to primary.

    The Debian convention for application-private shared objects and modules is:

        usr/lib/<multiarch-triplet>/<app>/
        usr/lib/<app>/

    These directories are exclusively owned by the primary package — they are
    not public ABI (that would live directly in usr/lib/<triplet>/ with proper
    symbol versioning) and they are not development artefacts.

    When the classifier places such files in a secondary bucket (e.g. *-other)
    because they lack a clear category signal, dpkg raises a file-overwrite
    conflict if the primary package also installs into the same directory
    (via a wildcard glob like 'usr/lib/x86_64-linux-gnu/enlightenment/*').

    The detection heuristic: an install-manifest entry is considered app-private
    when, after normalising leading slashes, it starts with 'usr/lib/' and
    contains '/<app_name>/' as a path-segment pair anywhere in the remaining
    components.  This naturally matches:

        usr/lib/x86_64-linux-gnu/enlightenment/modules/appmenu/*
        usr/lib/enlightenment/utils/*
        usr/lib/x86_64-linux-gnu/evisum/plugin.so

    without hardcoding any app name or triplet.
    """
    if primary_pkg not in install_manifests:
        return

    # The segment we look for: the app name as a path component.
    # We search for it preceded and followed by a slash so that e.g.
    # "enlightenment" does not accidentally match "enlightenment-extra".
    app_seg = f"/{app_name}/"
    # Also match the app name as the final component (no trailing slash).
    app_seg_end = f"/{app_name}"

    primary_entries: set[str] = set(install_manifests[primary_pkg])

    for pkg_name, entries in install_manifests.items():
        if pkg_name == primary_pkg:
            continue
        kept: list[str] = []
        for entry in entries:
            rel = entry.lstrip("/")
            if not rel.startswith("usr/lib/"):
                kept.append(entry)
                continue
            # Does the path contain /<app_name>/ (or end with /<app_name>) ?
            if app_seg not in rel and not rel.endswith(app_seg_end):
                kept.append(entry)
                continue
            # It is an app-private lib entry — promote to primary.
            if entry not in primary_entries:
                primary_entries.add(entry)
                info(
                    f"generator: promoted {entry!r} from {pkg_name!r} "
                    f"to primary package {primary_pkg!r} (app-lib policy)"
                )
            else:
                info(
                    f"generator: removed {entry!r} from {pkg_name!r} "
                    f"(already in primary {primary_pkg!r}; app-lib policy)"
                )

        install_manifests[pkg_name] = kept

    install_manifests[primary_pkg] = sorted(primary_entries)


def _promote_desktop_files_to_primary(
    primary_pkg: str,
    install_manifests: dict[str, list[str]],
) -> None:
    """Move all usr/share/applications/*.desktop entries to the primary package.

    Debian policy: a file must be owned by exactly one package.  Desktop
    entry files describe the primary application launcher and belong
    exclusively to the main runtime package.  When the classifier emits
    them into a secondary bucket (*-data, *-other, *-dev, …) the primary
    package often claims the same path via a wildcard glob, causing a
    dpkg file-overwrite conflict.

    Rules:
      - Any entry whose normalised path matches
        'usr/share/applications/*.desktop' (exact filename) or a glob
        that covers that directory is a desktop entry.
      - Such entries are removed from every secondary package manifest.
      - They are added to the primary package manifest (deduplicated).
      - The primary package manifest is re-sorted for stable output.
    """
    if primary_pkg not in install_manifests:
        return

    _DESKTOP_PREFIX = "usr/share/applications/"
    _DESKTOP_SUFFIX = ".desktop"

    def _is_desktop(entry: str) -> bool:
        rel = entry.lstrip("/")
        # Exact file: usr/share/applications/foo.desktop
        if rel.startswith(_DESKTOP_PREFIX) and rel.endswith(_DESKTOP_SUFFIX):
            return True
        # Wildcard glob emitted by _coalesce_to_dirs:
        # usr/share/applications/* — would cover .desktop files
        if rel == "usr/share/applications/*":
            return True
        return False

    primary_entries: set[str] = set(install_manifests[primary_pkg])
    changed = False

    for pkg_name, entries in install_manifests.items():
        if pkg_name == primary_pkg:
            continue
        kept: list[str] = []
        for entry in entries:
            if not _is_desktop(entry):
                kept.append(entry)
                continue
            # Desktop entry found in a secondary package — promote/remove.
            if entry not in primary_entries:
                primary_entries.add(entry)
                changed = True
                info(
                    f"generator: promoted {entry!r} from {pkg_name!r} "
                    f"to primary package {primary_pkg!r} (desktop-entry policy)"
                )
            else:
                info(
                    f"generator: removed {entry!r} from {pkg_name!r} "
                    f"(already in primary {primary_pkg!r}; desktop-entry policy)"
                )
        install_manifests[pkg_name] = kept

    if changed:
        install_manifests[primary_pkg] = sorted(primary_entries)


def _rebuild_special_files(
    output_packages: list[dict[str, Any]],
    install_manifests: dict[str, list[str]],
    plan_buckets: list[dict[str, Any]],
) -> None:
    """Reassign special files to output_packages based on final install manifests."""
    all_specials = []
    for b in plan_buckets:
        all_specials.extend(b.get("special_files", []))

    if not all_specials:
        return

    for pkg in output_packages:
        pkg["special_files"] = []

    for spec in all_specials:
        path = spec["path"]  # starts with '/' e.g. '/usr/bin/foo'
        rel_path = path.lstrip("/")

        assigned = False
        for pkg in output_packages:
            pkg_name = pkg["name"]
            manifest = install_manifests.get(pkg_name, [])

            owns = False
            for entry in manifest:
                entry_rel = entry.lstrip("/")
                if entry_rel.endswith("/*"):
                    prefix = entry_rel[:-2]
                    if rel_path.startswith(prefix + "/"):
                        owns = True
                        break
                elif entry_rel == rel_path:
                    owns = True
                    break

            if owns:
                pkg["special_files"].append(spec)
                assigned = True
                break

        if not assigned:
            info(f"warning: no package owns special file {path} after promotions; dropping special permissions")

    for pkg in output_packages:
        if pkg["special_files"]:
            pkg["special_files"].sort(key=lambda s: s["path"])
