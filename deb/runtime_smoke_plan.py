"""Runtime smoke plan generation for Python projects."""

import json
from pathlib import Path
from typing import Any

from deb.utils.log import info, error


def write_runtime_smoke_plan(meta: dict[str, Any], stage_dir: Path, orthos: Path) -> None:
    """Derive runtime smoke targets and write to runtime-smoke-plan.json.

    Only applies to python-pyproject backends. For others, writes no file.
    Does not execute anything. Never fails the package.
    """
    if meta.get("build_backend") != "python-pyproject":
        return

    targets = []
    seen_scripts = set()

    try:
        # 1. Derive console-script targets from meta["scripts"]
        scripts = meta.get("scripts", {})
        for script_name in sorted(scripts):
            targets.append({
                "kind": "console-script",
                "name": script_name,
                "command": [script_name, "--help"],
                "source": "project.scripts"
            })
            seen_scripts.add(script_name)

        # Add any other scripts staged in /usr/bin
        bin_dir = stage_dir / "usr" / "bin"
        if bin_dir.is_dir():
            for script_path in sorted(bin_dir.iterdir()):
                if script_path.is_file() and script_path.name not in seen_scripts:
                    targets.append({
                        "kind": "console-script",
                        "name": script_path.name,
                        "command": [script_path.name, "--help"],
                        "source": "staged /usr/bin"
                    })
                    seen_scripts.add(script_path.name)

        # 2. Derive import targets from top_level.txt
        seen_imports = set()
        for p in sorted(stage_dir.rglob("*.dist-info")):
            if p.is_dir():
                top_level = p / "top_level.txt"
                if top_level.is_file():
                    try:
                        lines = top_level.read_text(encoding="utf-8").splitlines()
                        for line in lines:
                            mod = line.strip()
                            if mod and mod.isidentifier() and mod not in seen_imports:
                                targets.append({
                                    "kind": "import",
                                    "name": mod,
                                    "command": ["python3", "-c", f"import {mod}"],
                                    "source": "dist-info/top_level.txt"
                                })
                                seen_imports.add(mod)
                    except OSError:
                        pass
    except Exception as exc:
        error(f"smoke: failed to fully derive targets: {exc}; proceeding with partial/empty target list")

    plan = {
        "build_backend": "python-pyproject",
        "targets": targets
    }

    try:
        plan_file = orthos / "runtime-smoke-plan.json"
        plan_file.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        info(f"smoke: wrote runtime smoke plan to {plan_file} ({len(targets)} targets)")
    except Exception as exc:
        error(f"smoke: failed to write runtime smoke plan: {exc}")
