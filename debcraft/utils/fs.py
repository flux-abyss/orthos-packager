"""Filesystem utilities."""

import json
from pathlib import Path


def ensure_dir(path: Path) -> None:
    """Create *path* and any missing parents (no-op if already present)."""
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, data: dict) -> None:
    """Write *data* as pretty-printed JSON to *path*."""
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
