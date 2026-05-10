"""Generate a minimal debian/ skeleton from a package-plan.json."""

from pathlib import Path
from typing import Any

from deb.generator.pkg_validator import validate_packages
from deb.generator.rules import _gen_meson_configure_override, _gen_rules
from deb.generator.changelog import _gen_changelog
from deb.generator.source import _gen_source_format
from deb.generator.copyright import _gen_copyright
from deb.generator.maintainer_scripts import _write_maintainer_scripts
from deb.generator.lintian import _write_lintian_overrides
from deb.paths import orthos_dir
from deb.resolution.debian import validate_extra_depends
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
from deb.generator.build_depends import _gen_build_depends
from deb.generator.runtime_depends import _runtime_dep_state
from deb.resolution.oracle import make_oracle
from deb.runtime_dependency_convergence import load_runtime_convergence_depends
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import info

_PLAN_FILE = "package-plan.json"
_RESULT_FILE = "generate-result.json"

_DEFAULT_MAINTAINER = "Unknown Maintainer <fixme@example.com>"
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
        info("suggestion: run 'orthos config init' to set your identity.")
        
    return res["identity"]


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
    orthos = orthos_dir(repo)
    plan_file = orthos / _PLAN_FILE

    plan = _load_plan(plan_file)
    build_backend = meta.get("build_backend", plan.get("build_backend", "meson"))

    # Debian package names use hyphens, not underscores.
    if build_backend == "python-pyproject" and meta.get("project_name"):
        app_name = meta["project_name"].replace("_", "-")
    else:
        app_name = repo_name.replace("_", "-")

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

    build_backend = meta.get("build_backend", plan.get("build_backend", "meson"))
    if build_backend == "python-pyproject":
        collapse = True
    else:
        collapse = _should_collapse(non_empty)

    primary = _primary_bucket_name(non_empty)
    build_depends, build_depends_source = _gen_build_depends(
        repo, oracle, build_backend=build_backend
    )
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

    # Load runtime-smoke-discovered dep candidates from the convergence state
    # file (written by write_runtime_convergence_state after a smoke failure).
    # Merge them into every package's extra_depends before apt validation so
    # they flow through the same verification path as statically inferred deps.
    # Packages for which no convergence state exists are unaffected.
    convergence_extra = load_runtime_convergence_depends(orthos)
    if convergence_extra:
        info(f"generate: runtime convergence extra depends: {convergence_extra}")
        for pkg in output_packages:
            existing = list(pkg.get("extra_depends", []))
            # Append only entries not already present, preserving order.
            seen_existing = set(existing)
            for dep in convergence_extra:
                if dep not in seen_existing:
                    existing.append(dep)
                    seen_existing.add(dep)
            pkg["extra_depends"] = existing

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
                     primary_section=primary_section, primary=primary, build_backend=build_backend),
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
    rules_project_name = (meta.get("project_name") or app_name) if build_backend == "python-pyproject" else app_name
    write_text(rules_path, _gen_rules(rules_overrides, build_backend=build_backend, project_name=rules_project_name))
    rules_path.chmod(0o755)

    write_text(
        debian_dir / "changelog",
        _gen_changelog(app_name, version, maintainer),
    )
    write_text(source_dir / "format", _gen_source_format())

    if not collapse and build_backend != "python-pyproject":
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
        "build_backend": meta.get("build_backend", plan.get("build_backend", "meson")),
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
