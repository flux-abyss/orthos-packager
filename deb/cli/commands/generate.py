"""generate command handler."""

from typing import Any

from deb.generator.debian_generator import generate as run_generate
from deb.utils.log import error, info


def _print_validation_summary(result: dict[str, Any]) -> None:
    """Print a concise inter-package and -dev validation summary."""
    iv = result.get("inter_pkg_validation")
    if iv:
        present = iv.get("present_primary_depends", [])
        missing = iv.get("missing_primary_depends", [])
        present_str = ", ".join(
            p.split("-", 1)[-1] for p in present) or "none"
        missing_str = ", ".join(
            p.split("-", 1)[-1] for p in missing) or "none"
        info("inter-package validation:")
        info(f"  primary depends ok: {present_str}")
        info(f"  missing:            {missing_str}")

    dv = result.get("dev_pkg_validation", [])
    if dv:
        info("dev package validation:")
        for rec in dv:
            lockstep = "ok" if rec["has_main_lockstep_dep"] else "MISSING"
            leakage = "yes (WARN)" if rec["has_shlibs_dep"] else "no"
            info(f"  {rec['package']} lockstep dep: {lockstep}")
            info(f"  {rec['package']} shlibs leakage: {leakage}")


def cmd_generate(
    repo_path: str,
    probe,
    chroot_path: str | None = None,
    meson_options: dict[str, str] | None = None,
) -> int:
    """Generate a debian/ skeleton from the package plan."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    if chroot_path is not None:
        meta["_chroot_path"] = chroot_path
    if meson_options:
        meta["meson_options"] = meson_options

    try:
        rc, result = run_generate(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"plan:    {result['plan_file']}")
    info(f"debian:  {result['debian_dir']}")
    info(f"files:   {len(result['generated_files'])}")
    for pkg in result["binary_packages"]:
        info(f"  {pkg}")
    info(f"wrote:   {result['debian_dir']}")

    _print_validation_summary(result)
    return rc
