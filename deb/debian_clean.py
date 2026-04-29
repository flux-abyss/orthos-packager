"""Remove dpkg-buildpackage byproducts from a debian/ tree.

Leaves only packaging source: control, rules, changelog, copyright,
source/, *.install, maintainer scripts, lintian overrides, and any
explicitly generated helper files.
"""

import shutil
from pathlib import Path

from deb.utils.log import info

# Directories created by debhelper during a build run.
_ARTIFACT_DIRS = {
    ".debhelper",
}

# File names that are always build byproducts.
_ARTIFACT_FILES = {
    "files",
    "debhelper-build-stamp",
}

# Suffixes that identify per-package build byproducts.
_ARTIFACT_SUFFIXES = {
    ".log",
    ".substvars",
}

# Known source file names that must never be removed.
_SOURCE_FILES = {
    "control",
    "rules",
    "changelog",
    "copyright",
    "compat",
    "watch",
}

# Known source directory names that must never be removed.
_SOURCE_DIRS = {
    "source",
}

# Known source suffixes that must never be removed.
_SOURCE_SUFFIXES = {
    ".install",
    ".dirs",
    ".docs",
    ".links",
    ".manpages",
    ".examples",
    ".triggers",
    ".lintian-overrides",
    ".postinst",
    ".preinst",
    ".postrm",
    ".prerm",
    ".conffiles",
    ".service",
    ".socket",
    ".timer",
}


def _is_package_payload_dir(path: Path, debian_dir: Path) -> bool:
    """Return True if *path* is a package payload tree written by dh_auto_install.

    These are top-level directories inside debian/ whose names match a binary
    package produced by the build (i.e. they contain a typical install prefix
    such as usr/ or etc/). They are never part of the packaging source.
    """
    if not path.is_dir():
        return False
    if path.name in _SOURCE_DIRS or path.name in _ARTIFACT_DIRS:
        return False
    if path.parent != debian_dir:
        return False

    fhs_roots = {"usr", "etc", "lib", "var", "opt", "srv", "run", "tmp"}
    return any((path / fhs).exists() for fhs in fhs_roots)


def clean_debian_tree(debian_dir: Path) -> None:
    """Remove build byproducts from *debian_dir*, preserving packaging source."""
    if not debian_dir.is_dir():
        return

    # 1. Known artifact directories (.debhelper, etc.)
    for name in _ARTIFACT_DIRS:
        target = debian_dir / name
        if target.exists():
            shutil.rmtree(target)
            info(f"clean: removed {name}/")

    # 2. Package payload trees (e.g. debian/<pkg>/ with usr/ inside)
    for child in sorted(debian_dir.iterdir()):
        if _is_package_payload_dir(child, debian_dir):
            shutil.rmtree(child)
            info(f"clean: removed payload dir {child.name}/")

    # 3. Known artifact files and suffixes
    for child in sorted(debian_dir.iterdir()):
        if child.is_dir():
            continue
        if child.name in _SOURCE_FILES:
            continue
        if child.suffix in _SOURCE_SUFFIXES:
            continue
        if child.suffix == "" and child.name in {
                "postinst",
                "preinst",
                "postrm",
                "prerm",
        }:
            continue
        if child.name in _ARTIFACT_FILES or child.suffix in _ARTIFACT_SUFFIXES:
            child.unlink()
            info(f"clean: removed {child.name}")


def clean_debian_build_artifacts(repo_path: Path) -> None:
    """Remove dpkg-buildpackage byproducts from *repo_path*/debian/.

    Safe to call multiple times; missing files and directories are silently
    ignored.  Does not touch any maintainer-authored packaging source.
    """
    clean_debian_tree(repo_path / "debian")
