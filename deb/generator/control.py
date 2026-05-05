"""Architecture detection and debian/control rendering for orthos generator."""

from typing import Any

from deb.generator.sections import _BUCKET_SECTION

# Buckets whose content is always architecture-independent.
# Every other bucket may contain compiled binaries, so it defaults to 'any'.
_ARCH_INDEPENDENT_BUCKETS = {"data", "doc"}


def _pkg_arch(pkg: dict[str, Any]) -> str:
    """Return 'all' for arch-independent packages, otherwise 'any'."""
    buckets: list[str] = pkg.get("buckets", [])
    if buckets and all(b in _ARCH_INDEPENDENT_BUCKETS for b in buckets):
        return "all"
    return "any"


def _gen_control(
    app_name: str,
    packages: list[dict[str, Any]],
    maintainer: str,
    build_depends: str,
    primary_section: str,
    primary: str | None = None,
) -> str:
    """Return debian/control content for the given packages."""
    lines: list[str] = [
        f"Source: {app_name}",
        f"Section: {primary_section}",
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
        pkg_buckets: list[str] = pkg.get("buckets", [])
        pkg_bucket = pkg_buckets[0] if pkg_buckets else (primary or "")

        if pkg_bucket in _BUCKET_SECTION:
            section = _BUCKET_SECTION[pkg_bucket]
        elif pkg_bucket in (primary, "data", ""):
            section = primary_section
        else:
            section = "misc"

        long_lines = []
        for line in long_desc.splitlines():
            s = line.strip()
            if not s:
                long_lines.append(" .")
            else:
                long_lines.append(f" {s}")
        formatted_long_desc = "\n".join(long_lines)

        lines += [
            f"Package: {pkg['name']}",
            f"Section: {section}",
            f"Architecture: {arch}",
            f"Depends: {', '.join(depends_parts)}",
            f"Description: {short_desc}",
            f"{formatted_long_desc}",
            "",
        ]

    return "\n".join(lines)
