"""Build dependency generation helpers for orthos generator."""

from pathlib import Path

from deb.dependency_hints import CURATED_BUILD_DEP_MAP, scan_meson_dependencies
from deb.resolution.debian import validate_build_depends_str
from deb.resolution.oracle import AptOracle

# Baseline build tools always required by the current packaging flow.
_BUILD_DEPENDS_BASE = "debhelper-compat (= 13), meson, ninja-build, pkgconf"


def _gen_build_depends(
    repo: Path,
    oracle: AptOracle,
    build_backend: str = "meson",
) -> tuple[str, str]:
    """Return (Build-Depends string, provenance label).

    Derives the package list from meson.build dependency() declarations
    when available; falls back to the static baseline.

    Raises ValueError for unrecognised *build_backend* values (non-Meson
    support is reserved for future milestones).
    """
    if build_backend != "meson":
        raise ValueError(
            f"_gen_build_depends: unsupported build_backend={build_backend!r}. "
            "Only 'meson' is supported in this release."
        )

    names = scan_meson_dependencies(repo)
    if not names:
        return _BUILD_DEPENDS_BASE, "control-default"

    # Map known names; unknown names are skipped (they go through chroot convergence resolution).
    extra: list[str] = []
    for name in names:
        pkg = CURATED_BUILD_DEP_MAP.get(name)
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

