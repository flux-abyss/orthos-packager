"""Generate a minimal debian/ skeleton from a package-plan.json."""

from pathlib import Path
from typing import Any

from deb.build_deps import BODHI_BUILD_DEP_MAP, scan_meson_dependencies
from deb.deps import infer_dependencies
from deb.generator.pkg_validator import validate_packages
from deb.generator.rules import _gen_meson_configure_override, _gen_rules
from deb.generator.changelog import _gen_changelog
from deb.generator.source import _gen_source_format
from deb.generator.copyright import _gen_copyright
from deb.generator.maintainer_scripts import _write_maintainer_scripts
from deb.generator.lintian import _write_lintian_overrides
from deb.paths import orthos_dir
from deb.resolution.debian import (
    resolve_runtime_dependencies,
    validate_extra_depends,
    validate_build_depends_str,
)
from deb.generator.plan import _load_plan, _non_empty_buckets
from deb.generator.naming import (
    _pkg_name,
    _primary_bucket_name,
    _should_collapse,
)
from deb.generator.manifests import (
    _check_duplicate_ownership,
    _gen_install,
)
from deb.generator.promotions import (
    _promote_etc_to_primary,
    _promote_app_lib_dirs_to_primary,
    _promote_desktop_files_to_primary,
    _rebuild_special_files,
)
from deb.generator.layout import _build_package_layout
from deb.generator.sections import _infer_primary_section
from deb.generator.control import _gen_control
from deb.resolution.oracle import AptOracle, make_oracle
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import info

_PLAN_FILE = "package-plan.json"
_RESULT_FILE = "generate-result.json"

_DEFAULT_MAINTAINER = "Unknown Maintainer <fixme@example.com>"
# Baseline build tools always required by the current packaging flow.
_BUILD_DEPENDS_BASE = "debhelper-compat (= 13), meson, ninja-build, pkgconf"
_VERSION_FALLBACK = "0.1.0"



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

    Uses meta["maintainer"] when present; falls back to configured user identity,
    then _DEFAULT_MAINTAINER.
    """
    m = meta.get("maintainer") or ""
    if m.strip():
        return m.strip()

    from deb.config import get_maintainer_identity_result
    res = get_maintainer_identity_result()
    if res["is_default"]:
        from deb.utils.log import info
        if res["reason"] == "invalid-config":
            info("warning: config file could not be read, using fallback.")
        else:
            info("warning: no maintainer configured, using fallback.")
        info("suggestion: run 'orthos-packager config init' to set your identity.")
        
    return res["identity"]




def _gen_build_depends(repo: Path, oracle: AptOracle) -> tuple[str, str]:
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
    raw_depends = ", ".join(all_parts)
    validated_depends = validate_build_depends_str(raw_depends, oracle)
    
    return validated_depends, "meson+map"


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
    oracle: AptOracle | None = None,
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

    if non_emitted_runtime_deps:
        info("leaving to shlibs: "
             f"{', '.join(non_emitted_runtime_deps)}")

    # Debian resolution layer: confirm every explicit runtime dep is a real
    # package in the selected apt oracle before it reaches debian/control.
    # ELF-dynamic deps are excluded from non_elf_deps and handled by
    # ${shlibs:Depends}.
    verified_deps = resolve_runtime_dependencies(
        non_elf_deps,
        oracle=oracle,
    )

    return dep_report, verified_deps, non_emitted_runtime_deps




# _build_package_layout is in deb.generator.layout (imported above)
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

    plan = _load_plan(plan_file)

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

    # Build oracle once for this run; every dependency validation call must
    # use the same target apt resolution.
    oracle = make_oracle(meta.get("_chroot_path"))

    dep_report, non_elf_deps, non_emitted_runtime_deps = _runtime_dep_state(
        repo,
        orthos,
        oracle=oracle,
    )

    primary = _primary_bucket_name(non_empty)
    collapse = _should_collapse(non_empty)

    build_depends, build_depends_source = _gen_build_depends(repo, oracle)
    info(f"build-depends source: {build_depends_source}")

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

    primary_pkg_name = _pkg_name(app_name, primary, primary) if primary else app_name

    generated_pkg_names = frozenset(p["name"] for p in output_packages)
    for pkg in output_packages:
        pkg["extra_depends"] = validate_extra_depends(
            pkg.get("extra_depends", []),
            generated_pkg_names,
            pkg_label=pkg["name"],
            oracle=oracle,
        )

    _promote_etc_to_primary(primary_pkg_name, install_manifests)
    _promote_app_lib_dirs_to_primary(app_name, primary_pkg_name, install_manifests)
    _promote_desktop_files_to_primary(primary_pkg_name, install_manifests)

    _rebuild_special_files(output_packages, install_manifests, plan["package_buckets"])

    validation = validate_packages(app_name, output_packages)

    primary_section = _infer_primary_section(plan["package_buckets"], meta)

    write_text(
        debian_dir / "control",
        _gen_control(app_name, output_packages, maintainer, build_depends,
                     primary_section=primary_section, primary=primary),
    )

    rules_path = debian_dir / "rules"
    rules_overrides = meta.get("rules_overrides", "").strip()
    meson_options: dict[str, str] = meta.get("meson_options") or {}
    configure_override = _gen_meson_configure_override(meson_options)
    if configure_override:
        # configure override goes first; existing rules_overrides appended after.
        combined_overrides = configure_override.rstrip()
        if rules_overrides:
            combined_overrides += "\n\n" + rules_overrides
        rules_overrides = combined_overrides
    write_text(rules_path, _gen_rules(rules_overrides))
    rules_path.chmod(0o755)

    write_text(
        debian_dir / "changelog",
        _gen_changelog(app_name, version, maintainer),
    )
    write_text(source_dir / "format", _gen_source_format())

    if not collapse:
        _check_duplicate_ownership(install_manifests)
        for pkg_info in output_packages:
            pname = pkg_info["name"]
            write_text(
                debian_dir / f"{pname}.install",
                _gen_install(install_manifests[pname]),
            )

    write_text(
        debian_dir / "copyright",
        _gen_copyright(app_name, maintainer, meta),
    )

    _write_maintainer_scripts(debian_dir, output_packages, meta, write_text)
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
        "meson_options": meson_options,
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
