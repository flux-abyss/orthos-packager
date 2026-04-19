"""Probe a local repository and return structured metadata."""

import re
import subprocess
from pathlib import Path

# Matches: project('name', ...) or project("name", ...)
_RE_PROJECT_NAME = re.compile(r"""project\s*\(\s*['"]([^'"]+)['"]""")

# Matches: version: '1.2.3' or version: "1.2.3"
_RE_VERSION = re.compile(r"""version\s*:\s*['"]([^'"]+)['"]""")


def _parse_meson_build(meson_file: Path) -> tuple[str | None, str | None]:
    """Return (name, version) parsed from meson.build, or (None, None)."""
    try:
        text = meson_file.read_text(encoding="utf-8")
    except OSError:
        return None, None

    name_match = _RE_PROJECT_NAME.search(text)
    version_match = _RE_VERSION.search(text)

    name = name_match.group(1) if name_match else None
    version = version_match.group(1) if version_match else None
    return name, version


def _git_version(repo: "Path") -> str | None:
    """Return a version string from the nearest git tag, or None.

    Uses 'git describe --tags --abbrev=0' which returns the nearest
    ancestor tag with no suffix.  Strips a leading 'v' prefix.
    Only called when meson.build has no version field.
    """
    try:
        result = subprocess.run(
            ["git", "-C",
             str(repo), "describe", "--tags", "--abbrev=0"],
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


def _apt_candidate_version(package: str) -> str | None:
    """Return the apt candidate version for *package* on the host, or None.

    Best-effort: silently returns None if apt-cache is unavailable or the
    package is not known to the host's configured sources. Used by probe()
    to establish the distro source anchor before any compatibility reasoning.
    """
    try:
        result = subprocess.run(
            ["apt-cache", "policy", package],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            candidate = stripped.split(":", 1)[1].strip()
            return candidate if candidate not in ("", "(none)") else None
    return None


def probe(repo_path: str) -> dict:
    """Probe *repo_path* and return a metadata dict.

    Raises SystemExit on fatal errors (non-existent path, not a Meson project).
    """
    path = Path(repo_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {path}")

    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")

    meson_file = path / "meson.build"
    if not meson_file.exists():
        raise ValueError(f"no meson.build found in {path}")

    name, version = _parse_meson_build(meson_file)
    has_debian = (path / "debian").is_dir()

    # Version precedence:
    #   1. meson.build  (already in 'version' from _parse_meson_build)
    #   2. git tag      (nearest ancestor tag, stripped of leading 'v')
    #   3. None         (generator will apply _VERSION_FALLBACK)
    version_source = "meson"
    if not version:
        version = _git_version(path)
        version_source = "git-tag" if version else "fallback"

    # Query the host apt sources for the candidate version of this project.
    # This is best-effort: returns None if the package name is unknown or
    # apt-cache is not available. The result anchors the distro recommendation
    # before any compatibility reasoning runs.
    distro_candidate: dict | None = None
    if name:
        candidate_ver = _apt_candidate_version(name)
        if candidate_ver:
            distro_candidate = {
                "package": name,
                "candidate_version": candidate_ver,
            }

    return {
        "debian_dir": has_debian,
        "distro_candidate": distro_candidate,
        "meson": True,
        "project_name": name,
        "repo_path": str(path),
        "version": version,
        "version_source": version_source,
    }
