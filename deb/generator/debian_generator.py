"""Generate a minimal debian/ skeleton from a package-plan.json."""

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from deb.build_deps import BODHI_BUILD_DEP_MAP, scan_meson_dependencies
from deb.deps import _record_reason, infer_dependencies
from deb.generator.inter_pkg import (
    dev_pkg_main_dep,
    script_command_deps,
    synthesize_intra_deps,
)
from deb.generator.pkg_validator import validate_packages
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import info

_PLAN_FILE = "package-plan.json"
_RESULT_FILE = "generate-result.json"

_DEFAULT_MAINTAINER = "Unknown Maintainer <fixme@example.com>"
# Baseline build tools always required by the current packaging flow.
_BUILD_DEPENDS_BASE = "debhelper-compat (= 13), meson, ninja-build, pkgconf"
_DEBIAN_REVISION = "1"
_VERSION_FALLBACK = "0.1.0"

# The bucket that carries the main executable.
_BIN_BUCKET = "bin"

# Buckets whose content is always architecture-independent.
# Every other bucket may contain compiled binaries, so it defaults to 'any'.
_ARCH_INDEPENDENT_BUCKETS = {"data", "doc"}

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


def _resolve_version(meta: dict[str, Any]) -> str:
    """Return the best available upstream version string.

    Priority (set by repo_probe.py, recorded in meta["version_source"]):
    1. meta["version"]  - parsed from meson.build project() call
    2. git tag          - nearest ancestor tag, 'v' prefix stripped
    3. _VERSION_FALLBACK - used only when neither source is available
    """
    v = meta.get("version") or ""
    return v.strip() if v.strip() else _VERSION_FALLBACK


def _resolve_maintainer(meta: dict[str, Any]) -> str:
    """Return the maintainer string to embed in generated files.

    Uses meta["maintainer"] when present; falls back to _DEFAULT_MAINTAINER.
    """
    m = meta.get("maintainer") or ""
    return m.strip() if m.strip() else _DEFAULT_MAINTAINER


def _load_plan(plan_file: Path) -> dict[str, Any]:
    """Read package-plan.json; raise FileNotFoundError if absent.

    Also raises ValueError if the plan contains zero files across all
    buckets, which indicates a failed or empty stage.
    """
    if not plan_file.exists():
        raise FileNotFoundError(f"package plan not found: {plan_file}\n"
                                f"Run 'orthos-packager classify <repo>' first.")
    data: dict[str, Any] = json.loads(plan_file.read_text(encoding="utf-8"))
    if data.get("total_files", 0) == 0:
        raise ValueError(
            f"package plan contains zero files: {plan_file}\n"
            f"The stage produced no installable output. Fix the build and\n"
            f"rerun 'orthos-packager stage' then 'orthos-packager classify'.")
    return data


def _non_empty_buckets(
        package_buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only buckets that contain at least one file."""
    return [b for b in package_buckets if b["file_count"] > 0]


def _primary_bucket_name(non_empty: list[dict[str, Any]]) -> str | None:
    """Return the name of the primary (executable-bearing) bucket.

    The bin bucket is always primary when it has content.  If there is no
    bin bucket, the first non-empty bucket in canonical order is used.
    """
    for b in non_empty:
        if b["name"] == _BIN_BUCKET:
            return _BIN_BUCKET
    return non_empty[0]["name"] if non_empty else None


def _should_collapse(non_empty: list[dict[str, Any]]) -> bool:
    """Return True when all non-empty buckets can be merged into one package.

    Collapse when the only non-empty buckets are the executable-bearing
    bucket and/or the data bucket - no shared libs, dev headers, doc,
    or other content that would justify a separate package.
    """
    names = {b["name"] for b in non_empty}
    return names <= {_BIN_BUCKET, "data"}


def _pkg_name(app_name: str, bucket_name: str, primary: str | None) -> str:
    """Return the Debian binary package name for *bucket_name*.

    The primary bucket (executable-bearing, or the sole non-empty bucket)
    is named after the application with no suffix.  All secondary buckets
    receive a hyphen-separated suffix: <app>-data, <app>-dev, etc.
    """
    if bucket_name == primary:
        return app_name
    return f"{app_name}-{bucket_name}"


def _merged_files(non_empty: list[dict[str, Any]]) -> list[str]:
    """Return a sorted, combined file list across all non-empty buckets."""
    all_files: list[str] = []
    for b in non_empty:
        all_files.extend(b["files"])
    return sorted(all_files)


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


def _gen_build_depends(repo: Path) -> tuple[str, str]:
    """Return (Build-Depends string, provenance label).

    Derives the package list from meson.build dependency() declarations
    when available; falls back to the static baseline.
    """
    names = scan_meson_dependencies(repo)
    if not names:
        return _BUILD_DEPENDS_BASE, "control-default"

    # Map known names; unknown names are skipped (they go through smoke resolution).
    extra: list[str] = []
    for name in names:
        pkg = BODHI_BUILD_DEP_MAP.get(name)
        if pkg and pkg not in extra:
            extra.append(pkg)

    if not extra:
        return _BUILD_DEPENDS_BASE, "control-default"

    # Merge base + extras, deduplicated, in stable order.
    base_parts = [p.strip() for p in _BUILD_DEPENDS_BASE.split(",")]
    all_parts = base_parts + [p for p in extra if p not in base_parts]
    return ", ".join(all_parts), "meson+map"


# Provenance labels that indicate dh_shlibdeps handles the dep better than
# an explicit Depends entry.  We emit these only via ${shlibs:Depends}.
# "elf-dynamic" = visible dynamic linkage discovered by ldd.
# Static linkage never reaches this set (ldd produces no output for it).
_SHLIBS_HANDLED_PROVENANCES = {"elf-dynamic", "inferred"}


def _non_elf_runtime_deps(dep_report: Any) -> list[str]:
    """Return inferred runtime deps that ${shlibs:Depends} does NOT cover.

    Includes only packages with non-ELF provenance: python-import,
    gi-namespace, subprocess.  ELF/ldd deps are left to dh_shlibdeps.
    Result is sorted for stable output.
    """
    result: list[str] = []
    for pkg in dep_report.sorted_depends():
        prov = dep_report.provenance.get(pkg, "inferred")
        if prov not in _SHLIBS_HANDLED_PROVENANCES:
            result.append(pkg)
    return result


def _runtime_dep_state(
    repo: Path,
    orthos: Path,
) -> tuple[Any, list[str], list[str]]:
    """Infer runtime deps and return report plus emitted/non-emitted lists."""
    stage_dir = orthos / "stage"
    dep_report = infer_dependencies(
        repo,
        stage_dir=stage_dir if stage_dir.exists() else None,
    )

    inferred_deps = dep_report.sorted_depends()

    # Split inferred deps by provenance for clear logging.
    elf_dynamic_deps = [
        pkg for pkg in inferred_deps
        if dep_report.provenance.get(pkg) == "elf-dynamic"
    ]
    explicit_deps = [
        pkg for pkg in inferred_deps
        if dep_report.provenance.get(pkg) not in _SHLIBS_HANDLED_PROVENANCES
    ]

    if elf_dynamic_deps:
        info("dynamic ELF deps (shlibs-handled): "
             + ", ".join(elf_dynamic_deps))
    if explicit_deps:
        info("explicit non-ELF deps: " + ", ".join(explicit_deps))
    if not inferred_deps:
        info("inferred depends: (none)")

    for pkg, pkg_reasons in dep_report.sorted_reasons():
        prov = dep_report.provenance.get(pkg, "inferred")
        info(f"  dep: {pkg} [{prov}] <- {'; '.join(pkg_reasons)}")

    non_elf_deps = _non_elf_runtime_deps(dep_report)
    non_emitted_runtime_deps = [
        pkg for pkg in inferred_deps if pkg not in non_elf_deps
    ]

    if non_elf_deps:
        info(f"emitting explicit runtime deps: {', '.join(non_elf_deps)}")
    if non_emitted_runtime_deps:
        info("leaving to shlibs: "
             f"{', '.join(non_emitted_runtime_deps)}")

    return dep_report, non_elf_deps, non_emitted_runtime_deps


def _pkg_arch(pkg: dict[str, Any]) -> str:
    """Return 'all' for arch-independent packages, otherwise 'any'."""
    buckets: list[str] = pkg.get("buckets", [])
    if buckets and all(b in _ARCH_INDEPENDENT_BUCKETS for b in buckets):
        return "all"
    return "any"


# Bucket-based description templates: (short, long).
# The primary/collapsed case is handled separately.
_BUCKET_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "data": ("{app} data", "Shared data files for {app}."),
    "dev": ("{app} development files", "Development files for {app}."),
    "doc": ("{app} documentation", "Documentation for {app}."),
    "runtime": ("{app} runtime libraries", "Shared libraries for {app}."),
}


def _pkg_descriptions(
    app_name: str,
    bucket_name: str,
    is_primary: bool,
    meta_short: str | None = None,
) -> tuple[str, str]:
    """Return default short and long descriptions for a package."""
    if is_primary:
        short = (meta_short.strip()
                 if meta_short and meta_short.strip() else app_name)
        long_ = f"Runtime package for {app_name}."
        return short, long_

    if bucket_name in _BUCKET_DESCRIPTIONS:
        short_tmpl, long_tmpl = _BUCKET_DESCRIPTIONS[bucket_name]
        return short_tmpl.format(app=app_name), long_tmpl.format(app=app_name)

    short = f"{app_name} {bucket_name}"
    long_ = f"{bucket_name.capitalize()} package for {app_name}."
    return short, long_


# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
def _build_package_layout(
    app_name: str,
    non_empty: list[dict[str, Any]],
    primary: str | None,
    collapse: bool,
    non_elf_deps: list[str],
    meta: dict[str, Any],
    stage_dir: Path | None = None,
    dep_report: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Return output package metadata and install manifests."""
    output_packages: list[dict[str, Any]] = []
    install_manifests: dict[str, list[str]] = {}

    if collapse:
        all_files = _merged_files(non_empty)
        short_desc, long_desc = _pkg_descriptions(
            app_name, "", is_primary=True, meta_short=meta.get("description"))
        cmd_deps = script_command_deps(stage_dir, all_files)
        all_extra = list(non_elf_deps)
        for pkg, reason in cmd_deps:
            if pkg not in all_extra:
                all_extra.append(pkg)
            if dep_report is not None:
                dep_report.depends.add(pkg)
                _record_reason(dep_report, pkg, reason,
                               provenance="script-command")
        output_packages.append({
            "name": app_name,
            "short_desc": short_desc,
            "long_desc": long_desc,
            "buckets": [],
            "extra_depends": all_extra,
        })
        install_manifests[app_name] = _coalesce_to_dirs(all_files, app_name)
        return output_packages, install_manifests

    # Compute intra-package deps (main pulls data, plugins, applicable other).
    intra_deps = synthesize_intra_deps(app_name, non_empty, primary)
    if intra_deps:
        info("intra-package deps: " +
             "; ".join(f"{k} -> {v}" for k, v in sorted(intra_deps.items())))

    runtime_pkg: str | None = None
    for bucket in non_empty:
        if bucket["name"] == "runtime":
            runtime_pkg = _pkg_name(app_name, "runtime", primary)
            break

    all_staged: frozenset[str] = frozenset(
        f for b in non_empty for f in b["files"])

    for bucket in non_empty:
        bname = bucket["name"]
        pname = _pkg_name(app_name, bname, primary)
        is_primary = bname == primary
        is_dev = bname == "dev"
        extra: list[str] = []

        # --- intra-package deps synthesized above ---
        for dep in intra_deps.get(pname, []):
            if dep not in extra:
                extra.append(dep)

        # -dev always depends on the main package at the same binary version.
        # It does NOT receive generic ELF runtime deps; shlibs are skipped
        # in _gen_control for dev packages.
        if is_dev:
            main_dep = dev_pkg_main_dep(app_name)
            if main_dep not in extra:
                extra.insert(0, main_dep)
            # Also depend on runtime shlib package when present.
            if runtime_pkg and runtime_pkg not in extra:
                extra.append(runtime_pkg)

        # Non-ELF runtime deps go to the primary package only (not -dev).
        if is_primary:
            for dep in non_elf_deps:
                if dep not in extra:
                    extra.append(dep)

        # Script command deps for this specific package's files.
        # Provenance is recorded immediately into dep_report at detection time.
        cmd_deps = script_command_deps(stage_dir, bucket.get("files", []))
        for pkg, reason in cmd_deps:
            if pkg not in extra:
                extra.append(pkg)
                info(f"  script-cmd dep: {pname} -> {pkg}")
            if dep_report is not None:
                dep_report.depends.add(pkg)
                _record_reason(dep_report, pkg, reason,
                               provenance="script-command")

        short_desc, long_desc = _pkg_descriptions(
            app_name,
            bname,
            is_primary=is_primary,
            meta_short=meta.get("description") if is_primary else None,
        )
        output_packages.append({
            "name": pname,
            "short_desc": short_desc,
            "long_desc": long_desc,
            "buckets": [bname],
            "extra_depends": extra,
            "is_dev": is_dev,
        })
        install_manifests[pname] = _coalesce_to_dirs(bucket["files"], app_name,
                                                     all_staged)

    return output_packages, install_manifests


def _gen_control(
    app_name: str,
    packages: list[dict[str, Any]],
    maintainer: str,
    build_depends: str,
) -> str:
    """Return debian/control content for the given packages."""
    lines: list[str] = [
        f"Source: {app_name}",
        "Section: misc",
        "Priority: optional",
        f"Maintainer: {maintainer}",
        f"Build-Depends: {build_depends}",
        "Standards-Version: 4.6.2",
        "",
    ]

    for pkg in packages:
        arch = _pkg_arch(pkg)
        # ${shlibs:Depends} is only meaningful for arch-specific packages that
        # contain ELF binaries processed by dh_shlibdeps.  Omit it for:
        #   - Architecture: all  (no ELF content by definition)
        #   - -dev packages      (headers/static libs; ELF deps come from main)
        if arch == "all" or pkg.get("is_dev"):
            depends_parts = ["${misc:Depends}"]
        else:
            depends_parts = ["${shlibs:Depends}", "${misc:Depends}"]
        depends_parts.extend(pkg.get("extra_depends", []))
        short_desc = pkg.get("short_desc", pkg["name"])
        long_desc = pkg.get("long_desc", f"{app_name} package.")

        lines += [
            f"Package: {pkg['name']}",
            f"Architecture: {arch}",
            f"Depends: {', '.join(depends_parts)}",
            f"Description: {short_desc}",
            f" {long_desc}",
            "",
        ]

    return "\n".join(lines)


def _gen_rules(rules_overrides: str = "") -> str:
    """Return debian/rules with optional override content appended.

    Always includes override_dh_shlibdeps with --ignore-missing-info so that
    dpkg-shlibdeps does not emit fabricated Debian package names derived from
    host-local shlibs registrations (e.g. a custom EFL build that registers
    'libefl' in the host dpkg database).  Without this, dh_shlibdeps would
    write shlibs:Depends=libefl (>= X.Y.Z) into the substvars file, producing
    a Depends entry that does not exist on the target Debian system.

    --ignore-missing-info silently skips any library whose shlibs data is
    absent from the dpkg database rather than fabricating an entry.  When the
    package is built inside a proper Debian chroot (where target libraries are
    registered with correct Debian package names), those entries flow through
    correctly and the override has no negative effect.
    """
    # The shlibdeps override is unconditional: it is harmless when building
    # against genuine Debian libraries and essential when building on a host
    # that has non-Debian libraries installed.
    _SHLIBDEPS_OVERRIDE = textwrap.dedent("""\
        override_dh_shlibdeps:
        \tdh_shlibdeps -- --ignore-missing-info
    """)
    base = textwrap.dedent("""\
        #!/usr/bin/make -f

        %:
        \tdh $@
    """)
    result = base + "\n" + _SHLIBDEPS_OVERRIDE
    extra = rules_overrides.strip()
    if extra:
        result += "\n" + extra + "\n"
    return result


def _now_rfc2822() -> str:
    """Return current UTC time formatted for Debian changelog."""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _gen_changelog(app_name: str, version: str, maintainer: str) -> str:
    """Return the full text of debian/changelog."""
    now = _now_rfc2822()
    deb_version = f"{version}-{_DEBIAN_REVISION}"
    return textwrap.dedent(f"""\
        {app_name} ({deb_version}) unstable; urgency=medium

          * Initial release.

         -- {maintainer}  {now}
    """)


def _gen_source_format() -> str:
    """Return the full text of debian/source/format."""
    return "3.0 (quilt)\n"


def _gen_install(files: list[str]) -> str:
    """Return the text of a .install file: one path per line."""
    return "\n".join(files) + "\n" if files else ""


def _check_duplicate_ownership(
    install_manifests: dict[str, list[str]],
) -> None:
    """Abort if any install path is claimed by more than one package.

    Builds a mapping of *install glob/path* -> list[package names] and raises
    ``RuntimeError`` if any entry is owned by more than one package.  This
    prevents overlapping file ownership in the generated .deb packages.

    Args:
        install_manifests: Mapping of package name to its list of install
            entries (paths or globs as produced by ``_coalesce_to_dirs``).

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
      - Any entry that starts with ``etc/`` or ``etc/*`` is an etc/ file.
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
    (via a wildcard glob like ``usr/lib/x86_64-linux-gnu/enlightenment/*``).

    The detection heuristic: an install-manifest entry is considered app-private
    when, after normalising leading slashes, it starts with ``usr/lib/`` and
    contains ``/<app_name>/`` as a path-segment pair anywhere in the remaining
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
        ``usr/share/applications/*.desktop`` (exact filename) or a glob
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


# Maps SPDX-ish identifiers (lowercase) to (Debian label, common-licenses path or None).
_LICENSE_MAP: dict[str, tuple[str, str | None]] = {
    "gpl-2": ("GPL-2", "GPL-2"),
    "gpl-2+": ("GPL-2+", "GPL-2"),
    "gpl-3": ("GPL-3", "GPL-3"),
    "gpl-3+": ("GPL-3+", "GPL-3"),
    "lgpl-2": ("LGPL-2", "LGPL-2"),
    "lgpl-2+": ("LGPL-2+", "LGPL-2"),
    "lgpl-2.1": ("LGPL-2.1", "LGPL-2.1"),
    "lgpl-2.1+": ("LGPL-2.1+", "LGPL-2.1"),
    "lgpl-3": ("LGPL-3", "LGPL-3"),
    "lgpl-3+": ("LGPL-3+", "LGPL-3"),
    "mit": ("MIT", None),
    "apache-2.0": ("Apache-2.0", None),
    "bsd-2-clause": ("BSD-2-clause", None),
    "bsd-3-clause": ("BSD-3-clause", None),
}


def _resolve_license(meta: dict[str, Any]) -> tuple[str, str]:
    """Return (Debian label, license text line) for the upstream license.

    Tries meta["license"] first.  Falls back to 'unknown' with a placeholder.
    """
    raw = (meta.get("license") or "").strip().lower()
    entry = _LICENSE_MAP.get(raw)
    if entry:
        label, common = entry
        if common:
            text = f" See /usr/share/common-licenses/{common}."
        else:
            text = f" {label} license."
        return label, text
    return "unknown", " See upstream source for license terms."


def _gen_copyright(app_name: str, maintainer: str, meta: dict[str, Any]) -> str:
    """Return a DEP-5 debian/copyright with a single Files: * stanza."""
    upstream_name = meta.get("project_name") or app_name
    source_url = meta.get("source_url") or "FIXME"
    year = datetime.now(timezone.utc).year
    license_label, license_text = _resolve_license(meta)

    return textwrap.dedent(f"""\
        Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
        Upstream-Name: {upstream_name}
        Upstream-Contact: FIXME
        Source: {source_url}

        Files: *
        Copyright: {year} FIXME <fixme@example.com>
        License: {license_label}
        {license_text}

        Files: debian/*
        Copyright: {year} {maintainer}
        License: {license_label}
        {license_text}
    """)


# Maintainer script files that may be emitted when content is explicitly
# provided in meta["maintainer_scripts"].
_MAINTAINER_SCRIPTS = ("postinst", "preinst", "prerm", "postrm")


def _write_maintainer_scripts(
    debian_dir: Path,
    meta: dict[str, Any],
    write_text_fn: Any,
) -> None:
    """Write maintainer scripts from meta["maintainer_scripts"]."""
    scripts: dict[str, str] = meta.get("maintainer_scripts") or {}
    for name in _MAINTAINER_SCRIPTS:
        content = scripts.get(name, "").strip()
        if not content:
            continue
        script_path = debian_dir / name
        write_text_fn(script_path, content + "\n")
        script_path.chmod(0o755)


def _write_lintian_overrides(
    debian_dir: Path,
    output_packages: list[dict[str, Any]],
    meta: dict[str, Any],
    write_text_fn: Any,
) -> None:
    """Write lintian override files from meta["lintian_overrides"]."""
    overrides: dict[str, str] = meta.get("lintian_overrides") or {}
    pkg_names = {p["name"] for p in output_packages}
    for pkg_name, content in overrides.items():
        if pkg_name not in pkg_names:
            continue
        content = content.strip()
        if not content:
            continue
        write_text_fn(debian_dir / f"{pkg_name}.lintian-overrides",
                      content + "\n")


# pylint: disable=too-many-locals


def _write_debian_helpers(
    debian_dir: Path,
    meta: dict[str, Any],
    write_text_fn: Any,
) -> None:
    """Write helper files from meta["debian_helpers"] into debian/."""
    helpers: dict[str, str] = meta.get("debian_helpers") or {}
    for filename, content in helpers.items():
        content = content.strip()
        if not content:
            continue
        file_path = debian_dir / filename
        write_text_fn(file_path, content + "\n")
        if filename.endswith(".sh"):
            file_path.chmod(0o755)


# pylint: disable=too-many-locals,too-many-statements
def generate(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Generate a debian/ skeleton from the package plan for *meta*.
    Returns (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    repo_name = repo.name
    # Debian package names use hyphens, not underscores.
    app_name = repo_name.replace("_", "-")
    orthos = orthos_dir(repo)
    plan_file = orthos / _PLAN_FILE

    plan = _load_plan(plan_file)  # raises FileNotFoundError if missing

    non_empty = _non_empty_buckets(plan["package_buckets"])
    debian_dir = orthos / "debian"
    source_dir = debian_dir / "source"

    for d in (debian_dir, source_dir):
        ensure_dir(d)

    generated: list[str] = []

    def write_text(rel: Path, content: str) -> None:
        rel.write_text(content, encoding="utf-8")
        generated.append(str(rel))

    version = _resolve_version(meta)
    maintainer = _resolve_maintainer(meta)
    version_source = meta.get("version_source", "unknown")
    info(f"version:    {version}  [{version_source}]")
    info(f"maintainer: {maintainer}")

    dep_report, non_elf_deps, non_emitted_runtime_deps = _runtime_dep_state(
        repo, orthos)

    primary = _primary_bucket_name(non_empty)
    collapse = _should_collapse(non_empty)

    # Derive Build-Depends from meson scan where possible.
    build_depends, build_depends_source = _gen_build_depends(repo)
    info(f"build-depends source: {build_depends_source}")

    # Build package descriptors and install manifests from classified buckets.
    _stage_dir = orthos / "stage"
    output_packages, install_manifests = _build_package_layout(
        app_name,
        non_empty,
        primary,
        collapse,
        non_elf_deps,
        meta,
        stage_dir=_stage_dir if _stage_dir.exists() else None,
        dep_report=dep_report,
    )

    # Enforce etc/ policy: files under etc/ must be owned by the primary
    # package only.  Run before duplicate-ownership check so that any
    # misclassified etc/ files are corrected rather than rejected.
    primary_pkg_name = _pkg_name(app_name, primary, primary) if primary else app_name
    _promote_etc_to_primary(primary_pkg_name, install_manifests)
    # Enforce app-private lib policy: usr/lib/**/<app>/** belongs to primary.
    _promote_app_lib_dirs_to_primary(app_name, primary_pkg_name, install_manifests)
    # Enforce desktop-entry policy: usr/share/applications/*.desktop belongs
    # to the primary package only.
    _promote_desktop_files_to_primary(primary_pkg_name, install_manifests)

    # Validate generated package relationships before writing files.
    validation = validate_packages(app_name, output_packages)

    # -- Write debian/ files ----------------------------------------------

    write_text(
        debian_dir / "control",
        _gen_control(app_name, output_packages, maintainer, build_depends))

    rules_path = debian_dir / "rules"
    rules_overrides = meta.get("rules_overrides", "").strip()
    write_text(rules_path, _gen_rules(rules_overrides))
    rules_path.chmod(0o755)

    write_text(debian_dir / "changelog",
               _gen_changelog(app_name, version, maintainer))
    write_text(source_dir / "format", _gen_source_format())

    # Emit .install files only when multiple packages are generated.
    # Single-package builds rely on dh_auto_install directly.
    if not collapse:
        # Guard: abort before writing any file if ownership conflicts exist.
        _check_duplicate_ownership(install_manifests)
        for pkg_info in output_packages:
            pname = pkg_info["name"]
            write_text(debian_dir / f"{pname}.install",
                       _gen_install(install_manifests[pname]))

    write_text(debian_dir / "copyright",
               _gen_copyright(app_name, maintainer, meta))

    _write_maintainer_scripts(debian_dir, meta, write_text)
    _write_lintian_overrides(debian_dir, output_packages, meta, write_text)
    _write_debian_helpers(debian_dir, meta, write_text)

    binary_packages = [p["name"] for p in output_packages]

    result: dict[str, Any] = {
        "binary_packages": binary_packages,
        "build_depends": build_depends,
        "build_depends_source": build_depends_source,
        "debian_dir": str(debian_dir),
        "dep_provenance": dep_report.provenance,
        "emitted_runtime_deps": non_elf_deps,
        "non_emitted_runtime_deps": non_emitted_runtime_deps,
        "generated_files": generated,
        "plan_file": str(plan_file),
        "repo_path": str(repo),
        "version_source": version_source,
        "inter_pkg_validation": validation["inter_pkg_validation"],
        "dev_pkg_validation": validation["dev_pkg_validation"],
    }

    write_json(orthos / _RESULT_FILE, result)
    return 0, result
