"""Package layout builder for orthos generator."""

from pathlib import Path
from typing import Any

from deb.runtime_dependency_inference import _record_reason
from deb.generator.descriptions import _pkg_descriptions
from deb.generator.inter_pkg import (
    dev_pkg_main_dep,
    script_command_deps,
    synthesize_intra_deps,
)
from deb.generator.manifests import _coalesce_to_dirs
from deb.generator.naming import _merged_files, _pkg_name
from deb.utils.log import info


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
            app_name, "", is_primary=True, meta=meta)
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
            "special_files": [],
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
            meta=meta if is_primary else None,
        )
        output_packages.append({
            "name": pname,
            "short_desc": short_desc,
            "long_desc": long_desc,
            "buckets": [bname],
            "extra_depends": extra,
            "is_dev": is_dev,
            "special_files": [],
        })
        install_manifests[pname] = _coalesce_to_dirs(bucket["files"], app_name,
                                                     all_staged)

    return output_packages, install_manifests
