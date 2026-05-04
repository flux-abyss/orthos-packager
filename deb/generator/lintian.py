"""Write lintian override files for orthos-generated packages."""

from pathlib import Path
from typing import Any


def _write_lintian_overrides(
    debian_dir: Path,
    output_packages: list[dict[str, Any]],
    meta: dict[str, Any],
    write_text_fn: Any,
) -> None:
    """Write lintian override files from meta["lintian_overrides"]."""
    overrides: dict[str, str] = meta.get("lintian_overrides") or {}
    pkg_names = {p["name"] for p in output_packages}
    for pkg_name, content in overrides.items():
        if pkg_name not in pkg_names:
            continue
        content = content.strip()
        if not content:
            continue
        write_text_fn(debian_dir / f"{pkg_name}.lintian-overrides",
                      content + "\n")
