"""Probe a local repository and return structured metadata."""

import subprocess
from pathlib import Path

from deb.backends.registry import detect_backend


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

    Raises FileNotFoundError, NotADirectoryError, or ValueError on fatal
    errors (non-existent path, not a directory, unrecognised build system).

    The returned dict always contains:
        repo_path        - resolved absolute path string
        debian_dir       - True if a debian/ directory exists
        distro_candidate - apt candidate version info dict, or None
    plus all keys returned by the detected backend's scan_metadata().
    For the Meson backend that includes: build_backend, meson,
    project_name, version, version_source.
    """
    path = Path(repo_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"path does not exist: {path}")

    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")

    # Detect build backend via registry (raises ValueError for unsupported repos).
    backend = detect_backend(path)

    # Collect all build-system-specific fields from the adapter.
    backend_meta = backend.scan_metadata(path)

    # Generic fields that every backend shares.
    has_debian = (path / "debian").is_dir()

    # Query the host apt sources for the candidate version of this project.
    # project_name comes from the backend; best-effort, silently None.
    project_name = backend_meta.get("project_name")
    distro_candidate: dict | None = None
    if project_name:
        candidate_ver = _apt_candidate_version(project_name)
        if candidate_ver:
            distro_candidate = {
                "package": project_name,
                "candidate_version": candidate_ver,
            }

    meta: dict = {
        "debian_dir": has_debian,
        "distro_candidate": distro_candidate,
        "repo_path": str(path),
    }
    # Merge backend-specific fields last so they take precedence where keys
    # overlap (build_backend, meson, project_name, version, version_source).
    meta.update(backend_meta)
    return meta
