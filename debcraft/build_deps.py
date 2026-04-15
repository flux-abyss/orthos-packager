"""Build dependency inference and resolution for Meson projects.

Scans meson.build files for dependency() declarations, resolves each name to
an installable Debian package, and prefers Bodhi-native packages over generic
Debian/Ubuntu ones.  Resolution order:

  1. Bodhi mapping table  (curated, deterministic)
  2. Already-installed package satisfying the need
  3. Bodhi apt candidate
  4. Fallback apt candidate from any enabled repo
  5. Unresolved  →  logged clearly, pipeline aborts

After the first-pass Meson build-dep install, a second pass validates the
pkg-config closure and installs any transitive pkg-config dependencies that
were not implied directly by the Meson names (e.g. lua51 pulled in by edje).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Bodhi-first mapping table
# ---------------------------------------------------------------------------
# Keys are Meson dependency() names (lowercase); values are Debian package
# names.  Bodhi-native entries are marked with "bodhi" in the source field.

BODHI_BUILD_DEP_MAP: dict[str, str] = {
    # EFL / Elementary
    "elementary": "libefl-dev",
    "efl": "libefl-dev",
    "ecore": "libefl-dev",
    "ecore-ipc": "libefl-dev",
    "ecore-file": "libefl-dev",
    "ecore-evas": "libefl-dev",
    "ecore-con": "libefl-dev",
    "ecore-input": "libefl-dev",
    "ecore-audio": "libefl-dev",
    "ecore-imf": "libefl-dev",
    "ecore-x": "libefl-dev",
    "evas": "libefl-dev",
    "edje": "libefl-dev",
    "eina": "libefl-dev",
    "eo": "libefl-dev",
    "efreet": "libefl-dev",
    "eio": "libefl-dev",
    "eldbus": "libefl-dev",
    "ethumb": "libefl-dev",
    "emotion": "libefl-dev",
    "eet": "libefl-dev",
    "emile": "libefl-dev",
    # Common system deps often seen alongside EFL projects
    "glib-2.0": "libglib2.0-dev",
    "gobject-2.0": "libglib2.0-dev",
    "gio-2.0": "libglib2.0-dev",
    "dbus-1": "libdbus-1-dev",
    "openssl": "libssl-dev",
    "zlib": "zlib1g-dev",
    "libpng": "libpng-dev",
    "libjpeg": "libjpeg-dev",
    "freetype2": "libfreetype6-dev",
    "fontconfig": "libfontconfig1-dev",
    "x11": "libx11-dev",
    "xcb": "libxcb1-dev",
}

# URL fragments that identify a Bodhi-native apt repository origin.
_BODHI_ORIGINS = ("bodhilinux.com",)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ResolutionResult:
    """Outcome for a single Meson dependency name."""

    meson_name: str
    package: str | None  # resolved Debian package name, or None
    source: str  # "bodhi_map", "installed", "bodhi_apt", "apt_fallback", "unresolved"
    is_installed: bool = False
    is_bodhi: bool = False
    warning: str | None = None


@dataclass
class BuildDependencyReport:
    """Summary of build dependency discovery and resolution for one project."""

    discovered: list[str] = field(default_factory=list)
    results: list[ResolutionResult] = field(default_factory=list)

    def missing_packages(self) -> list[str]:
        """Return sorted list of resolved but not-yet-installed packages."""
        return sorted({
            r.package for r in self.results if r.package and not r.is_installed
        })

    def unresolved_names(self) -> list[str]:
        """Return sorted list of Meson names we could not resolve at all."""
        return sorted(r.meson_name for r in self.results if r.package is None)


# ---------------------------------------------------------------------------
# Meson file scanning
# ---------------------------------------------------------------------------

# Pragmatic regex: captures the first string argument of dependency(...)
# Handles both single and double quotes, optional trailing options.
_DEP_RE = re.compile(r"""dependency\(\s*['"]([^'"]+)['"]""")


def scan_meson_dependencies(repo: Path) -> list[str]:
    """Return sorted unique Meson dependency names declared in *repo*.

    Scans meson.build and any meson.build files in subdirectories.
    """
    names: set[str] = set()
    for meson_file in repo.rglob("meson.build"):
        try:
            text = meson_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in _DEP_RE.finditer(text):
            names.add(match.group(1).strip().lower())
    return sorted(names)


# ---------------------------------------------------------------------------
# apt / dpkg helpers
# ---------------------------------------------------------------------------


def _is_installed(package: str) -> bool:
    """Return True when *package* is currently installed on the system."""
    result = subprocess.run(
        ["dpkg", "-s", package],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "Status: install ok installed" in result.stdout


def _apt_cache_policy(package: str) -> tuple[bool, bool]:
    """Return (exists_in_apt, is_bodhi_candidate) for *package*.

    *exists_in_apt* is True when apt knows the package at all.
    *is_bodhi_candidate* is True when the candidate version comes from a
    Bodhi-native repository.
    """
    result = subprocess.run(
        ["apt-cache", "policy", package],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False, False

    lines = result.stdout.splitlines()
    # Look for "Candidate: <something>" - if it is "(none)", package unknown.
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            candidate = stripped.split(":", 1)[1].strip()
            if candidate in ("", "(none)"):
                return False, False
            break
    else:
        return False, False

    # Check whether any version table entry for the candidate originates from
    # a Bodhi repo.
    is_bodhi = any(
        any(origin in line for origin in _BODHI_ORIGINS) for line in lines)
    return True, is_bodhi


def _apt_search_dev(meson_name: str) -> str | None:
    """Best-effort: ask apt-cache to find a -dev package for *meson_name*.

    Returns the first plausible package name, or None.
    """
    # Try lib<name>-dev heuristic first (covers many common C libraries).
    candidate = f"lib{meson_name}-dev"
    exists, _ = _apt_cache_policy(candidate)
    if exists:
        return candidate

    # Fall back to a keyword apt-cache search.
    result = subprocess.run(
        ["apt-cache", "search", "--names-only", f"{meson_name}.*-dev"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        pkg = line.split()[0]
        if pkg:
            return pkg
    return None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_build_dependency(name: str) -> ResolutionResult:
    """Resolve one Meson dependency name to a Debian package.

    See module docstring for resolution order.
    """
    # 1. Bodhi mapping table
    mapped = BODHI_BUILD_DEP_MAP.get(name)
    if mapped:
        installed = _is_installed(mapped)
        _, is_bodhi = _apt_cache_policy(mapped)
        return ResolutionResult(
            meson_name=name,
            package=mapped,
            source="bodhi_map",
            is_installed=installed,
            is_bodhi=is_bodhi,
        )

    # 2. Check if a lib<name>-dev style package is already installed
    naive = f"lib{name}-dev"
    if _is_installed(naive):
        _, is_bodhi = _apt_cache_policy(naive)
        return ResolutionResult(
            meson_name=name,
            package=naive,
            source="installed",
            is_installed=True,
            is_bodhi=is_bodhi,
        )

    # 3 + 4. apt candidate search - prefer Bodhi origin
    apt_pkg = _apt_search_dev(name)
    if apt_pkg:
        installed = _is_installed(apt_pkg)
        _, is_bodhi = _apt_cache_policy(apt_pkg)
        source = "bodhi_apt" if is_bodhi else "apt_fallback"
        warning = (
            None if is_bodhi else
            f"using non-Bodhi fallback for dependency '{name}' -> {apt_pkg}")
        return ResolutionResult(
            meson_name=name,
            package=apt_pkg,
            source=source,
            is_installed=installed,
            is_bodhi=is_bodhi,
            warning=warning,
        )

    # 5. Unresolved
    return ResolutionResult(
        meson_name=name,
        package=None,
        source="unresolved",
        warning=(f"could not resolve build dependency '{name}': "
                 f"not in Bodhi map, not installed as lib{name}-dev, "
                 f"apt-cache found no candidate"),
    )


def resolve_build_dependencies(names: list[str]) -> BuildDependencyReport:
    """Resolve every Meson dependency name in *names* and return a report."""
    report = BuildDependencyReport(discovered=list(names))
    for name in names:
        report.results.append(resolve_build_dependency(name))
    return report


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def install_missing_build_dependencies(report: BuildDependencyReport) -> int:
    """Install missing packages from *report* using apt.

    Returns 0 on success, non-zero on failure.
    """
    missing = report.missing_packages()
    if not missing:
        return 0
    result = subprocess.run(
        ["sudo", "apt", "install", "-y", *missing],
        check=False,
    )
    return result.returncode


# ---------------------------------------------------------------------------
# pkg-config closure
# ---------------------------------------------------------------------------

# Maps pkg-config module names (as they appear in pkg-config error output) to
# the Debian package that provides the missing .pc file.
BODHI_PKGCONFIG_MAP: dict[str, str] = {
    # EFL transitive deps surfaced by pkg-config errors during evisum build
    "lua51": "liblua5.1-0-dev",
    "lua5.1": "liblua5.1-0-dev",
    "luajit": "libluajit-5.1-dev",
    # Common extras that show up as pkg-config transitive deps
    "fribidi": "libfribidi-dev",
    "harfbuzz": "libharfbuzz-dev",
    "pixman-1": "libpixman-1-dev",
    "libpulse": "libpulse-dev",
    "alsa": "libasound2-dev",
    "gl": "libgl-dev",
    "glesv2": "libgles2-mesa-dev",
    "egl": "libegl-dev",
    "libinput": "libinput-dev",
    "libudev": "libudev-dev",
    "libdrm": "libdrm-dev",
    "xcb-xfixes": "libxcb-xfixes0-dev",
    "xcb-render": "libxcb-render0-dev",
    "xcb-shape": "libxcb-shape0-dev",
}

# Two error-message patterns from pkg-config --print-errors:
#   Package 'lua51', required by 'edje', not found
#   Package lua51 was not found in the pkg-config search path.
#   No package 'luajit' found
_PKGCFG_MISSING_RE = re.compile(r"Package '([^']+)'.*not found"
                                r"|Package ([^\s']+) was not found"
                                r"|No package '([^']+)' found")


@dataclass
class PkgConfigClosureReport:
    """Result of a pkg-config closure validation pass."""

    checked: list[str] = field(default_factory=list)
    passing: list[str] = field(default_factory=list)
    # pkg-config name -> (required_by, resolved_package | None)
    missing: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retries: int = 0

    def all_satisfied(self) -> bool:
        """True when every checked name passed pkg-config validation."""
        return not self.missing and not self.unresolved


def extract_missing_pkgconfig_names(output: str) -> list[str]:
    """Parse pkg-config stderr and return a sorted list of missing module names."""
    found: set[str] = set()
    for m in _PKGCFG_MISSING_RE.finditer(output):
        # Groups 1, 2, or 3 depending on which pattern matched.
        name = m.group(1) or m.group(2) or m.group(3)
        if name and name != "virtual:world":
            found.add(name)
    return sorted(found)


def _pkg_config_check(name: str) -> tuple[bool, str]:
    """Run pkg-config --exists on *name*.

    Returns (passed, combined_stderr_output).
    """
    result = subprocess.run(
        ["pkg-config", "--exists", "--print-errors", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0, result.stderr


def resolve_pkgconfig_dependency(name: str) -> ResolutionResult:
    """Resolve a pkg-config module name to a Debian package.

    Resolution order mirrors the Meson dep resolver:
      1. BODHI_PKGCONFIG_MAP
      2. lib<name>-dev already installed
      3. apt-cache search
    """
    mapped = BODHI_PKGCONFIG_MAP.get(name)
    if mapped:
        installed = _is_installed(mapped)
        _, is_bodhi = _apt_cache_policy(mapped)
        return ResolutionResult(
            meson_name=name,
            package=mapped,
            source="bodhi_map",
            is_installed=installed,
            is_bodhi=is_bodhi,
        )

    naive = f"lib{name}-dev"
    if _is_installed(naive):
        _, is_bodhi = _apt_cache_policy(naive)
        return ResolutionResult(
            meson_name=name,
            package=naive,
            source="installed",
            is_installed=True,
            is_bodhi=is_bodhi,
        )

    apt_pkg = _apt_search_dev(name)
    if apt_pkg:
        installed = _is_installed(apt_pkg)
        _, is_bodhi = _apt_cache_policy(apt_pkg)
        source = "bodhi_apt" if is_bodhi else "apt_fallback"
        warning = (
            None if is_bodhi else
            f"using non-Bodhi fallback for pkg-config dep '{name}' -> {apt_pkg}"
        )
        return ResolutionResult(
            meson_name=name,
            package=apt_pkg,
            source=source,
            is_installed=installed,
            is_bodhi=is_bodhi,
            warning=warning,
        )

    return ResolutionResult(
        meson_name=name,
        package=None,
        source="unresolved",
        warning=(f"could not resolve pkg-config dep '{name}': "
                 f"not in Bodhi map, not installed as lib{name}-dev, "
                 f"apt-cache found no candidate"),
    )


_MAX_CLOSURE_RETRIES = 5


def validate_pkg_config_closure(names: list[str]) -> PkgConfigClosureReport:
    """Validate that every name in *names* passes pkg-config --exists.

    When a check fails, extracted missing module names are resolved and
    returned in the report.  Actual installation is the caller's job;
    call this again after installing to verify closure.
    """
    report = PkgConfigClosureReport(checked=list(names))
    pending = list(names)
    seen_missing: set[str] = set()

    for _ in range(_MAX_CLOSURE_RETRIES):
        still_failing: list[str] = []
        new_missing: dict[str, tuple[str, str | None]] = {}

        for name in pending:
            passed, stderr = _pkg_config_check(name)
            if passed:
                if name not in report.passing:
                    report.passing.append(name)
                continue

            still_failing.append(name)
            for missing_name in extract_missing_pkgconfig_names(stderr):
                if missing_name in seen_missing:
                    continue
                seen_missing.add(missing_name)
                res = resolve_pkgconfig_dependency(missing_name)
                pkg = res.package
                new_missing[missing_name] = (name, pkg)
                if res.warning:
                    report.warnings.append(res.warning)

        report.missing.update(new_missing)

        # If no new missing names surfaced, we are done (pass or stuck).
        if not new_missing:
            break

        report.retries += 1
        pending = still_failing

    # Any names still failing after all retries are unresolved.
    for name in pending:
        passed, _ = _pkg_config_check(name)
        if not passed and name not in report.passing:
            report.unresolved.append(name)

    return report


def install_missing_pkgconfig_dependencies(
        report: PkgConfigClosureReport) -> int:
    """Install packages that were found missing during pkg-config closure.

    Returns 0 on success, non-zero on apt failure.
    """
    to_install: list[str] = sorted({
        pkg for (_, pkg) in report.missing.values()
        if pkg and not _is_installed(pkg)
    })
    if not to_install:
        return 0
    result = subprocess.run(
        ["sudo", "apt", "install", "-y", *to_install],
        check=False,
    )
    return result.returncode
