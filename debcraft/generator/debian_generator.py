"""Generate a minimal debian/ skeleton from a package-plan.json."""

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from debcraft.deps import infer_dependencies
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import info

_PLAN_FILE = "package-plan.json"
_RESULT_FILE = "generate-result.json"

_DEFAULT_MAINTAINER = "FIXME <fixme@example.com>"
_BUILD_DEPENDS = "debhelper-compat (= 13), meson, ninja-build, pkgconf"
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
    # Shared data install areas.
    "usr/share/applications",
    "usr/share/icons",
    "usr/share/locale",
    "usr/share/man",
    "usr/share/pixmaps",
    # System config area.
    "etc",
)


def _orthos_dir(repo_path: Path) -> Path:
    """Mirror the layout used by all earlier steps."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


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


def _pkg_arch(pkg: dict[str, Any]) -> str:
    """Return 'all' for arch-independent packages, otherwise 'any'."""
    buckets: list[str] = pkg.get("buckets", [])
    if buckets and all(b in _ARCH_INDEPENDENT_BUCKETS for b in buckets):
        return "all"
    return "any"


# Bucket-based description templates: (short, long).
# The primary/collapsed case is handled separately.
_BUCKET_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "data":    ("{app} data",                 "Shared data files for {app}."),
    "dev":     ("{app} development files",    "Development files for {app}."),
    "doc":     ("{app} documentation",        "Documentation for {app}."),
    "plugins": ("{app} plugins",              "Plugin files for {app}."),
    "runtime": ("{app} runtime libraries",   "Shared libraries for {app}."),
}


def _pkg_descriptions(
    app_name: str,
    bucket_name: str,
    is_primary: bool,
    meta_short: str | None = None,
) -> tuple[str, str]:
    """Return default short and long descriptions for a package."""
    if is_primary:
        short = (meta_short.strip() if meta_short and meta_short.strip()
                 else app_name)
        long_ = f"Runtime package for {app_name}."
        return short, long_

    if bucket_name in _BUCKET_DESCRIPTIONS:
        short_tmpl, long_tmpl = _BUCKET_DESCRIPTIONS[bucket_name]
        return short_tmpl.format(app=app_name), long_tmpl.format(app=app_name)

    short = f"{app_name} {bucket_name}"
    long_ = f"{bucket_name.capitalize()} package for {app_name}."
    return short, long_


def _gen_control(
    app_name: str,
    packages: list[dict[str, Any]],
    maintainer: str,
) -> str:
    """Return debian/control content for the given packages."""
    lines: list[str] = [
        f"Source: {app_name}",
        "Section: misc",
        "Priority: optional",
        f"Maintainer: {maintainer}",
        f"Build-Depends: {_BUILD_DEPENDS}",
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

          * Initial packaging.

         -- {maintainer}  {now}
    """)


def _gen_source_format() -> str:
    """Return the full text of debian/source/format."""
    return "3.0 (quilt)\n"


def _gen_install(files: list[str]) -> str:
    """Return the text of a .install file: one path per line."""
    return "\n".join(files) + "\n" if files else ""


def _gen_copyright(app_name: str, maintainer: str, meta: dict[str, Any]) -> str:
    """Return debian/copyright content."""
    upstream_name = meta.get("project_name") or app_name
    source_url = meta.get("source_url") or "FIXME"
    year = datetime.now(timezone.utc).year

    return textwrap.dedent(f"""\
        Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
        Upstream-Name: {upstream_name}
        Upstream-Contact: FIXME
        Source: {source_url}

        Files: *
        Copyright: {year} FIXME <FIXME@example.com>
        License: FIXME
         FIXME: Replace with the actual license text or a short-form
         reference such as GPL-2+ or MIT.  See /usr/share/common-licenses/.

        Files: debian/*
        Copyright: {year} {maintainer}
        License: FIXME
         FIXME: Replace with the license that applies to the packaging.
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


# pylint: disable=too-many-locals
def generate(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Generate a debian/ skeleton from the package plan for *meta*.
    Returns (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    repo_name = repo.name
    # Debian package names use hyphens, not underscores.
    app_name = repo_name.replace("_", "-")
    orthos = _orthos_dir(repo)
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
    info(f"version:    {version}")
    info(f"maintainer: {maintainer}")

    # Dependency inference is kept for log output, but is not injected
    # into generated control stanzas.  ${shlibs:Depends} at build time
    # is more accurate for ELF-bearing packages.
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

    primary = _primary_bucket_name(non_empty)
    collapse = _should_collapse(non_empty)

    # -- Build the package descriptor list and install manifests ----------

    # output_packages: ordered list of dicts passed to _gen_control
    output_packages: list[dict[str, Any]] = []
    # install_manifests: pkg_name -> coalesced file list
    install_manifests: dict[str, list[str]] = {}

    if collapse:
        # Single-package layout: merge all buckets into <app>.
        # all_staged is omitted - files IS the complete tree, so the
        # exclusivity check in _coalesce_to_dirs passes trivially.
        all_files = _merged_files(non_empty)
        # Collapsed packages always carry compiled content; buckets=[] → 'any'.
        short_desc, long_desc = _pkg_descriptions(
            app_name, "", is_primary=True,
            meta_short=meta.get("description"))
        output_packages.append({
            "name": app_name,
            "short_desc": short_desc,
            "long_desc": long_desc,
            "buckets": [],
            "extra_depends": [],
        })
        install_manifests[app_name] = _coalesce_to_dirs(all_files, app_name)

    else:
        # Multi-package layout: primary gets <app>, secondaries get suffixes.
        # If a data companion package exists, wire it into the primary Depends.
        data_companion: str | None = None
        for bucket in non_empty:
            if bucket["name"] == "data" and bucket["name"] != primary:
                data_companion = _pkg_name(app_name, "data", primary)

        # Build the complete staged file set once so _coalesce_to_dirs can
        # verify that a collapsed directory is not shared across packages.
        all_staged: frozenset[str] = frozenset(
            f for b in non_empty for f in b["files"])

        for bucket in non_empty:
            bname = bucket["name"]
            pname = _pkg_name(app_name, bname, primary)
            is_primary = (bname == primary)
            extra: list[str] = []
            if is_primary and data_companion:
                extra.append(data_companion)
            short_desc, long_desc = _pkg_descriptions(
                app_name, bname, is_primary=is_primary,
                meta_short=meta.get("description") if is_primary else None)
            output_packages.append({
                "name": pname,
                "short_desc": short_desc,
                "long_desc": long_desc,
                "buckets": [bname],
                "extra_depends": extra,
            })
            install_manifests[pname] = _coalesce_to_dirs(
                bucket["files"], app_name, all_staged)

    # -- Write debian/ files ----------------------------------------------

    write_text(debian_dir / "control",
               _gen_control(app_name, output_packages, maintainer))

    rules_path = debian_dir / "rules"
    rules_overrides = (meta.get("rules_overrides") or "").strip()
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
        "debian_dir": str(debian_dir),
        "generated_files": generated,
        "plan_file": str(plan_file),
        "repo_path": str(repo),
    }

    write_json(orthos / _RESULT_FILE, result)
    return 0, result
