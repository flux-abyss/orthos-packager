"""Load and filter package-plan.json for orthos generator."""

import json
from pathlib import Path
from typing import Any


def _load_plan(plan_file: Path) -> dict[str, Any]:
    """Read package-plan.json; raise FileNotFoundError if absent.

    Also raises ValueError if the plan contains zero files across all
    buckets, which indicates a failed or empty stage.
    """
    if not plan_file.exists():
        raise FileNotFoundError(f"package plan not found: {plan_file}\n"
                                f"Run 'orthos classify <repo>' first.")
    data: dict[str, Any] = json.loads(plan_file.read_text(encoding="utf-8"))
    if data.get("total_files", 0) == 0:
        raise ValueError(
            f"package plan contains zero files: {plan_file}\n"
            f"The stage produced no installable output. Fix the build and\n"
            f"rerun 'orthos stage' then 'orthos classify'.")
    return data


def _non_empty_buckets(
        package_buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only buckets that contain at least one file."""
    return [b for b in package_buckets if b["file_count"] > 0]
