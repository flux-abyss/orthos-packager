"""Probe a local repository and return structured metadata."""

import re
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

    return {
        "debian_dir": has_debian,
        "meson": True,
        "project_name": name,
        "repo_path": str(path),
        "version": version,
    }
