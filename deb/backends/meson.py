"""Meson build-backend adapter for the orthos backend registry."""

import re
import subprocess
from pathlib import Path
from typing import Any

import deb.backends.build_backend_meson as _impl

name = "meson"

# Matches: project('name', ...) or project("name", ...)
_RE_PROJECT_NAME = re.compile(r"""project\s*\(\s*['"]([^'"]+)['"]""")

# Matches: version: '1.2.3' or version: "1.2.3"
_RE_VERSION = re.compile(r"""version\s*:\s*['"]([^'"]+)['"]""")


def can_handle(repo: Path) -> bool:
    """Return True when *repo* contains a meson.build file."""
    return (repo / "meson.build").exists()


def _parse_meson_build(meson_file: Path) -> tuple[str | None, str | None]:
    """Return (name, version) parsed from meson.build, or (None, None)."""
    try:
        text = meson_file.read_text(encoding="utf-8")
    except OSError:
        return None, None

    name_match = _RE_PROJECT_NAME.search(text)
    version_match = _RE_VERSION.search(text)

    project_name = name_match.group(1) if name_match else None
    version = version_match.group(1) if version_match else None
    return project_name, version


def _git_version(repo: Path) -> str | None:
    """Return a version string from the nearest git tag, or None.

    Uses 'git describe --tags --abbrev=0' which returns the nearest
    ancestor tag with no suffix.  Strips a leading 'v' prefix.
    Only called when meson.build has no version field.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag.lstrip("v") if tag else None


def scan_metadata(repo: Path) -> dict[str, Any]:
    """Return Meson-specific metadata for *repo*.

    Keys returned:
        build_backend  - always "meson"
        meson          - always True (legacy compat flag)
        project_name   - parsed from meson.build project() call, or None
        version        - from meson.build, git tag, or None
        version_source - "meson" | "git-tag" | "fallback"
    """
    project_name, version = _parse_meson_build(repo / "meson.build")

    # Version precedence:
    #   1. meson.build project() version: field
    #   2. nearest ancestor git tag (stripped of leading 'v')
    #   3. None  (generator will apply its own _VERSION_FALLBACK)
    version_source = "meson"
    if not version:
        version = _git_version(repo)
        version_source = "git-tag" if version else "fallback"

    return {
        "build_backend": name,
        "meson": True,
        "project_name": project_name,
        "version": version,
        "version_source": version_source,
    }


def stage(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run the Meson staging flow described by *meta*.

    Delegates entirely to the existing implementation so behaviour is
    preserved exactly.
    """
    return _impl.stage(meta)

