"""Generate a minimal debian/ skeleton from a package-plan.json."""

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from debcraft.build_deps import BODHI_BUILD_DEP_MAP, scan_meson_dependencies
from debcraft.deps import infer_dependencies
from debcraft.paths import orthos_dir
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import info

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

    Priority:
    1. meta["version"] - set by the scan/probe step from meson.build
    2. _VERSION_FALLBACK - used only when no version is discoverable
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
    """Read package-plan.json; raise FileNotFoundError if absent."""
    if not plan_file.exists():
        raise FileNotFoundError(f"package plan not found: {plan_file}\n"
                                f"Run 'orthos-packager classify <repo>' first.")
    data: dict[str, Any] = json.loads(plan_file.read_text(encoding="utf-8"))
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
    bucket and/or the data bucket - no shared libs, dev headers, plugins,
    doc, or other content that would justify a separate package.
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
    return ", ".join(all_parts), "meson+bodhi-map"


# Provenance labels that indicate dh_shlibdeps handles the dep better than
# an explicit Depends entry.  We emit these only via ${shlibs:Depends}.
_SHLIBS_HANDLED_PROVENANCES = {"elf-ldd", "inferred"}


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
    dep_summary = ", ".join(inferred_deps) if inferred_deps else "(none)"
    info(f"inferred depends: {dep_summary}")
    for pkg, pkg_reasons in dep_report.sorted_reasons():
        info(f"  inferred reason: {pkg} <- {'; '.join(pkg_reasons)}")

    non_elf_deps = _non_elf_runtime_deps(dep_report)
    non_emitted_runtime_deps = [
        pkg for pkg in inferred_deps if pkg not in non_elf_deps
    ]

    if non_elf_deps:
        info(f"emitting non-ELF runtime deps: {', '.join(non_elf_deps)}")
    if non_emitted_runtime_deps:
        info("skipping non-emitted runtime deps: "
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
    "plugins": ("{app} plugins", "Plugin files for {app}."),
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


# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
def _build_package_layout(
    app_name: str,
    non_empty: list[dict[str, Any]],
    primary: str | None,
    collapse: bool,
    non_elf_deps: list[str],
    meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Return output package metadata and install manifests."""
    output_packages: list[dict[str, Any]] = []
    install_manifests: dict[str, list[str]] = {}

    if collapse:
        all_files = _merged_files(non_empty)
        short_desc, long_desc = _pkg_descriptions(
            app_name, "", is_primary=True, meta_short=meta.get("description"))
        output_packages.append({
            "name": app_name,
            "short_desc": short_desc,
            "long_desc": long_desc,
            "buckets": [],
            "extra_depends": list(non_elf_deps),
        })
        install_manifests[app_name] = _coalesce_to_dirs(all_files, app_name)
        return output_packages, install_manifests

    data_companion: str | None = None
    for bucket in non_empty:
        if bucket["name"] == "data" and bucket["name"] != primary:
            data_companion = _pkg_name(app_name, "data", primary)

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
        extra: list[str] = []

        if is_primary and data_companion:
            extra.append(data_companion)
        if bname == "dev" and runtime_pkg:
            extra.append(runtime_pkg)
        if is_primary:
            for dep in non_elf_deps:
                if dep not in extra:
                    extra.append(dep)

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
        depends_parts = ["${shlibs:Depends}", "${misc:Depends}"]
        depends_parts.extend(pkg.get("extra_depends", []))
        arch = _pkg_arch(pkg)
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
    """Return debian/rules with optional override content appended."""
    base = textwrap.dedent("""\
        #!/usr/bin/make -f

        %:
        \tdh $@
    """)
    overrides = rules_overrides.strip()
    if not overrides:
        return base
    return base + "\n" + overrides + "\n"


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
    output_packages, install_manifests = _build_package_layout(
        app_name,
        non_empty,
        primary,
        collapse,
        non_elf_deps,
        meta,
    )

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
    }

    write_json(orthos / _RESULT_FILE, result)
    return 0, result
