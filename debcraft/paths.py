"""Canonical path helpers shared across all pipeline stages."""

from pathlib import Path


def orthos_dir(repo_path: Path) -> Path:
    """Return the .orthos workspace directory for a repository."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name
