"""Build dependency inference and resolution for Meson projects.

scan_meson_dependencies() is the static hint-layer provider for the
convergence scaffold in deb.discovery.convergence.  It is no longer
the sole source of truth for build dependency decisions.

Scans meson.build files for dependency() declarations, resolves each name to
an installable Debian package, and prefers Bodhi-native packages over generic
Debian/Ubuntu ones.  Resolution order:

  1. Bodhi mapping table  (curated, deterministic)
  2. Already-installed package satisfying the need
  3. Bodhi apt candidate
  4. Fallback apt candidate from any enabled repo
  5. Unresolved - logged; the convergence loop handles stall conditions

BODHI_PKGCONFIG_MAP and validate_pkg_config_closure remain available for
standalone use or future post-apply pkg-config verification passes.
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
    # EFL / Elementary  (all modules ship in Bodhi's monolithic libefl-dev)
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
    "ecore-fb": "libefl-dev",
    "evas": "libefl-dev",
    "edje": "libefl-dev",
    "eina": "libefl-dev",
    "eo": "libefl-dev",
    "efreet": "libefl-dev",
    "eio": "libefl-dev",
    "eldbus": "libefl-dev",
    "ethumb": "libefl-dev",
    "ethumb-client": "libefl-dev",
    "emotion": "libefl-dev",
    "eet": "libefl-dev",
    "emile": "libefl-dev",
    "eeze": "libefl-dev",
    "eolian": "libefl-dev",
    "embryo": "libefl-dev",
    "elua": "libefl-dev",
    # GLib ecosystem
    "glib-2.0": "libglib2.0-dev",
    "gobject-2.0": "libglib2.0-dev",
    "gio-2.0": "libglib2.0-dev",
    "gmodule-2.0": "libglib2.0-dev",
    "gthread-2.0": "libglib2.0-dev",
    "gio-unix-2.0": "libglib2.0-dev",
    # GTK / GDK
    "gtk+-3.0": "libgtk-3-dev",
    "gtk4": "libgtk-4-dev",
    "gdk-pixbuf-2.0": "libgdk-pixbuf-2.0-dev",
    "pango": "libpango1.0-dev",
    "pangocairo": "libpango1.0-dev",
    "cairo": "libcairo2-dev",
    "cairo-gobject": "libcairo2-dev",
    "harfbuzz": "libharfbuzz-dev",
    # DBus / IPC
    "dbus-1": "libdbus-1-dev",
    "dbus-glib-1": "libdbus-glib-1-dev",
    # System / crypto
    "openssl": "libssl-dev",
    "libssl": "libssl-dev",
    "zlib": "zlib1g-dev",
    "libz": "zlib1g-dev",
    "liblzma": "liblzma-dev",
    "bzip2": "libbz2-dev",
    "libzstd": "libzstd-dev",
    # Image / media
    "libpng": "libpng-dev",
    "libpng16": "libpng-dev",
    "libjpeg": "libjpeg-dev",
    "libjpeg8": "libjpeg-dev",
    "libwebp": "libwebp-dev",
    "libavcodec": "libavcodec-dev",
    "libavformat": "libavformat-dev",
    "libavutil": "libavutil-dev",
    "libswscale": "libswscale-dev",
    # Fonts / text
    "freetype2": "libfreetype6-dev",
    "fontconfig": "libfontconfig1-dev",
    "fribidi": "libfribidi-dev",
    # X11 / display
    "x11": "libx11-dev",
    "xcb": "libxcb1-dev",
    "xext": "libxext-dev",
    "xrender": "libxrender-dev",
    "xfixes": "libxfixes-dev",
    "xi": "libxi-dev",
    "xtst": "libxtst-dev",
    "sm": "libsm-dev",
    "ice": "libice-dev",
    # Wayland
    "wayland-client": "libwayland-dev",
    "wayland-server": "libwayland-dev",
    "wayland-protocols": "wayland-protocols",
    "xkbcommon": "libxkbcommon-dev",
    # Sound
    "alsa": "libasound2-dev",
    "libpulse": "libpulse-dev",
    "libpulse-simple": "libpulse-dev",
    "pipewire": "libpipewire-0.3-dev",
    "pipewire-0.3": "libpipewire-0.3-dev",
    # Input / udev
    "libinput": "libinput-dev",
    "libudev": "libudev-dev",
    "libevdev": "libevdev-dev",
    "gudev-1.0": "libgudev-1.0-dev",
    # DRM / GL
    "libdrm": "libdrm-dev",
    "gl": "libgl-dev",
    "glesv2": "libgles2-mesa-dev",
    "egl": "libegl-dev",
    "gbm": "libgbm-dev",
    # SQLite / DB
    "sqlite3": "libsqlite3-dev",
    # Misc common
    "expat": "libexpat1-dev",
    "libxml-2.0": "libxml2-dev",
    "libxslt": "libxslt1-dev",
    "json-c": "libjson-c-dev",
    "jansson": "libjansson-dev",
    "yaml-0.1": "libyaml-dev",
    "libcurl": "libcurl4-openssl-dev",
    "libffi": "libffi-dev",
    "pcre": "libpcre3-dev",
    "pcre2": "libpcre2-dev",
    "threads": "libpthread-stubs0-dev",
    # System authentication / session
    "pam": "libpam0g-dev",
    "libpam": "libpam0g-dev",
    "systemd": "libsystemd-dev",
    "libsystemd": "libsystemd-dev",
    "libudev": "libudev-dev",
    # Network / avahi
    "avahi-client": "libavahi-client-dev",
    "avahi-core": "libavahi-core-dev",
    "libnl-3.0": "libnl-3-dev",
    "libnl-genl-3.0": "libnl-genl-3-dev",
    # Block / mount
    "mount": "libmount-dev",
    "blkid": "libblkid-dev",
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

# Captures cc.find_library('name') and bare find_library('name') calls.
# These surface setup-time requirements that dependency() never sees.
_FIND_LIB_RE = re.compile(
    r"""(?:cc|compiler|meson)\.find_library\(\s*['"]([^'"]+)['"]"""
    r"""|\bfind_library\(\s*['"]([^'"]+)['"]""")

# Generic libc / toolchain symbols that find_library() may name but should
# never become explicit Debian Build-Depends entries.  dh_shlibdeps handles
# these, and their -dev packages are pulled in transitively anyway.
_FIND_LIB_SKIP = {
    "m", "c", "rt", "dl", "pthread", "math", "execinfo",
    "stdc++", "gcc", "gcc_s", "atomic",
    "resolv", "nsl", "util",
}

# Option names from meson_options.txt: only captured if they are already
# a key in BODHI_BUILD_DEP_MAP.  Option names are project-local; the only
# reliable external-dep signal is map membership.
_OPT_RE = re.compile(r"""option\(\s*['"]([^'"]+)['"]""")


def _scan_file_for_pattern(
    path: "Path",
    pattern: re.Pattern[str],
) -> list[str]:
    """Return all non-empty lowercased group captures from *pattern* in *path*."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[str] = []
    for m in pattern.finditer(text):
        for g in m.groups():
            if g:
                found.append(g.strip().lower())
                break
    return found


def scan_meson_dependencies(repo: Path) -> list[str]:
    """Return sorted unique external dependency names for *repo*.

    Three sources, each with its own precision filter:

    1. dependency('name') - external pkg-config/cmake deps by Meson convention.
       Names shorter than 2 characters are dropped (single-char noise).

    2. cc.find_library('name') / find_library('name') - explicit library
       link requirements.  Generic libc/toolchain names are filtered out.

    3. option names from meson_options.txt / meson.options - gated to map
       membership only.  Project-internal feature toggles produce false
       positives when collected openly; map membership is the reliable
       signal that an option name represents a real external dep.

    All sources use the same lowercase name space and feed the same
    resolve/install path.
    """
    names: set[str] = set()

    # 1 + 2: scan every meson.build in the repo tree.
    for meson_file in repo.rglob("meson.build"):
        # dependency() - drop names shorter than 2 chars.
        for name in _scan_file_for_pattern(meson_file, _DEP_RE):
            if len(name) >= 2:
                names.add(name)
        # find_library() - drop generic libc/toolchain symbols.
        for name in _scan_file_for_pattern(meson_file, _FIND_LIB_RE):
            if name not in _FIND_LIB_SKIP:
                names.add(name)

    # 3: option names - only keep names already in BODHI_BUILD_DEP_MAP.
    for opts_name in ("meson_options.txt", "meson.options"):
        opts_file = repo / opts_name
        if opts_file.exists():
            for name in _scan_file_for_pattern(opts_file, _OPT_RE):
                if name in BODHI_BUILD_DEP_MAP:
                    names.add(name)

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
