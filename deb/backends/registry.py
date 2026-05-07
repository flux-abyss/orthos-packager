"""Backend registry: detect and retrieve build-system adapters."""

from pathlib import Path
from typing import Any

import deb.backends.meson as _meson
import deb.backends.python_pyproject as _python_pyproject

# Ordered list of registered adapters.  The first adapter whose can_handle()
# returns True wins during auto-detection.  Meson is checked first so that
# projects which wrap a Meson build in pyproject.toml are handled correctly.
_ADAPTERS = [_meson, _python_pyproject]

_BY_NAME: dict[str, Any] = {a.name: a for a in _ADAPTERS}


def detect_backend(repo: Path) -> Any:
    """Return the first registered adapter that can handle *repo*.

    Raises ValueError when no registered backend recognises the repository.
    """
    for adapter in _ADAPTERS:
        if adapter.can_handle(repo):
            return adapter
    raise ValueError(
        f"no supported build backend detected in {repo}. "
        "Supported backends: meson (meson.build), "
        "python-pyproject (pyproject.toml + setuptools.build_meta)."
    )


def get_backend(name: str) -> Any:
    """Return the adapter registered under *name*.

    Raises KeyError when *name* is not registered.
    """
    if name not in _BY_NAME:
        raise KeyError(
            f"unknown build backend: {name!r}. "
            f"Registered backends: {sorted(_BY_NAME)}"
        )
    return _BY_NAME[name]
