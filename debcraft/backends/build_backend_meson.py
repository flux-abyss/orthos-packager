"""Meson staging backend: setup -> compile -> install into a DESTDIR."""

import os
from pathlib import Path
from typing import Any

from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.shell import run_logged

_RESULT_FILE = "stage-result.json"
StageResult = dict[str, Any]


def _orthos_dir(repo_path: Path) -> Path:
    """Return the scratch directory for a target repository."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def stage(meta: dict[str, Any]) -> tuple[int, StageResult]:
    """Run the full Meson staging flow for the repo described by *meta*.

    Directories created under <repo>/.orthos/:
        build/ - Meson build tree
        stage/ - DESTDIR install root
        logs/ - combined build log

    Returns:
        A tuple of (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    orthos = _orthos_dir(repo)

    build_dir = orthos / "build"
    stage_dir = orthos / "stage"
    logs_dir = orthos / "logs"

    for directory in (build_dir, stage_dir, logs_dir):
        ensure_dir(directory)

    log_file = logs_dir / "stage.log"
    log_file.write_text("", encoding="utf-8")

    success = True
    failure_step: str | None = None

    ok, _ = run_logged(
        ["meson", "setup", str(build_dir),
         str(repo)],
        log_file=log_file,
    )
    if not ok:
        success = False
        failure_step = "meson setup"

    if success:
        ok, _ = run_logged(
            ["meson", "compile", "-C", str(build_dir)],
            log_file=log_file,
        )
        if not ok:
            success = False
            failure_step = "meson compile"

    if success:
        env = {**os.environ, "DESTDIR": str(stage_dir)}
        ok, _ = run_logged(
            ["meson", "install", "-C", str(build_dir)],
            log_file=log_file,
            env=env,
        )
        if not ok:
            success = False
            failure_step = "meson install"

    result: StageResult = {
        "build_dir": str(build_dir),
        "log_file": str(log_file),
        "project_name": meta.get("project_name"),
        "repo_path": str(repo),
        "stage_dir": str(stage_dir),
        "success": success,
        "version": meta.get("version"),
    }
    if failure_step is not None:
        result["failure_step"] = failure_step

    write_json(orthos / _RESULT_FILE, result)
    return (0 if success else 1), result
