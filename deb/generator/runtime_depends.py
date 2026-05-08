"""Runtime dependency state helpers for orthos generator."""

from pathlib import Path
from typing import Any

from deb.runtime_dependency_inference import infer_dependencies
from deb.resolution.debian import resolve_runtime_dependencies
from deb.resolution.oracle import AptOracle
from deb.utils.log import info

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
