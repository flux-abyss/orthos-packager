"""Python pyproject.toml (setuptools.build_meta) backend adapter.

Detection and metadata extraction only.  Staging/building is not
implemented in this milestone and will raise NotImplementedError.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

name = "python-pyproject"

# The only setuptools build-backend token recognised in this milestone.
_SETUPTOOLS_BUILD_META = "setuptools.build_meta"


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return its contents as a dict.

    Uses stdlib tomllib (Python >=3.11).  On Python 3.10 the import will
    fail with a clear ImportError explaining the Debian dependency.
    """
    if sys.version_info >= (3, 11):
        import tomllib  # noqa: PLC0415
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    else:
        # Python 3.10: tomllib is not in stdlib.  Attempt tomli (common
        # third-party shim) and surface a clear error if unavailable.
        try:
            import tomli as tomllib  # type: ignore[no-redef]  # noqa: PLC0415
        except ModuleNotFoundError:
            raise ImportError(
                "Python 3.10 detected: 'tomli' is required to parse pyproject.toml. "
                "Install the Debian package python3-tomli, or run Orthos with Python >= 3.11."
            ) from None
        with open(path, "rb") as fh:
            return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Registry protocol
# ---------------------------------------------------------------------------

def can_handle(repo: Path) -> bool:
    """Return True when *repo* has a pyproject.toml using setuptools.build_meta.

    Checks:
    1. pyproject.toml exists.
    2. [build-system].build-backend == "setuptools.build_meta".

    Poetry, Hatch, Flit, and custom backends are intentionally excluded.
    """
    toml_path = repo / "pyproject.toml"
    if not toml_path.exists():
        return False
    try:
        data = _load_toml(toml_path)
    except Exception:  # noqa: BLE001 — malformed TOML, wrong Python, etc.
        return False
    build_sys = data.get("build-system", {})
    return build_sys.get("build-backend") == _SETUPTOOLS_BUILD_META


def scan_metadata(repo: Path) -> dict[str, Any]:
    """Return pyproject metadata for *repo*.

    Keys always returned:
        build_backend        - "python-pyproject"
        python               - True
        python_build_backend - "setuptools.build_meta"
        project_name         - [project].name, or None
        version              - [project].version, or None
        version_source       - "pyproject" when version found, else "fallback"

    Optional keys (only present when the field exists in pyproject.toml):
        description          - [project].description
        requires_python      - [project].requires-python
        scripts              - {name: entrypoint, …} from [project.scripts]
    """
    toml_path = repo / "pyproject.toml"
    data = _load_toml(toml_path)
    project = data.get("project", {})

    project_name: str | None = project.get("name") or None
    version: str | None = project.get("version") or None
    version_source = "pyproject" if version else "fallback"

    meta: dict[str, Any] = {
        "build_backend": name,
        "python": True,
        "python_build_backend": _SETUPTOOLS_BUILD_META,
        "project_name": project_name,
        "version": version,
        "version_source": version_source,
    }

    description: str | None = project.get("description") or None
    if description is not None:
        meta["description"] = description

    requires_python: str | None = project.get("requires-python") or None
    if requires_python is not None:
        meta["requires_python"] = requires_python

    scripts: dict[str, str] | None = project.get("scripts") or None
    if scripts is not None:
        meta["scripts"] = dict(scripts)

    return meta


def stage(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Raise NotImplementedError — Python staging is not yet implemented."""
    raise NotImplementedError(
        "stage() is not supported for build_backend='python-pyproject'. "
        "Python project packaging is not implemented in this release."
    )
