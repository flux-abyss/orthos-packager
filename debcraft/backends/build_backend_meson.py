"""Meson staging backend: setup -> compile -> install into a DESTDIR."""

import os
from pathlib import Path
from typing import Any

from debcraft.expert.compat import evaluate_compile_failure
from debcraft.paths import orthos_dir
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.shell import run_logged

_RESULT_FILE = "stage-result.json"
StageResult = dict[str, Any]

# System paths that must appear first so Meson finds system Python, not venv.
_SYSTEM_PATH_PREPEND = ["/usr/bin", "/bin"]


def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with the active venv stripped from PATH.

    Ensures Meson subprocesses discover system Python rather than any
    interpreter embedded in the caller's virtual environment.
    """
    env = dict(os.environ)
    venv = env.get("VIRTUAL_ENV", "")
    venv_bin = os.path.join(venv, "bin") if venv else ""

    parts = env.get("PATH", "").split(os.pathsep)
    parts = [p for p in parts if p and p != venv_bin]
    for d in reversed(_SYSTEM_PATH_PREPEND):
        if d not in parts:
            parts.insert(0, d)

    env["PATH"] = os.pathsep.join(parts)
    return env


# Known host include roots used by the system compiler.
# stage() runs meson compile on the host, so expert analysis of compile
# failures must use host headers — not chroot headers from prior convergence.
_HOST_INCLUDE_CANDIDATES: list[str] = [
    "/usr/include",
    "/usr/include/x86_64-linux-gnu",
]


def _stage_include_roots() -> list[str]:
    """Return host include paths that actually exist on this system.

    Used by the expert compat rule to confirm whether a missing symbol is
    genuinely absent from the installed headers the compiler can see.
    Only paths that exist are returned; missing directories are silently
    skipped so the rule degrades gracefully on non-standard layouts.
    """
    return [p for p in _HOST_INCLUDE_CANDIDATES if Path(p).is_dir()]


def _next_step_strategy(verdicts: list[dict]) -> dict | None:
    """Return a structured strategy dict if verdicts indicate a mode switch.

    Returns None when the verdicts do not require a strategy change (i.e.
    ordinary dependency resolution should continue).
    """
    ids = {v.get("rule_id") for v in verdicts}
    if "source_too_new_for_target_api" in ids:
        return {
            "next_mode": "compatibility_search",
            "compatibility_strategy": "prefer_tag_or_release",
            "compatibility_reason": "source_too_new_for_target_api",
        }
    return None


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
    orthos = orthos_dir(repo)

    build_dir = orthos / "build"
    stage_dir = orthos / "stage"
    logs_dir = orthos / "logs"

    for directory in (build_dir, stage_dir, logs_dir):
        ensure_dir(directory)

    log_file = logs_dir / "stage.log"
    log_file.write_text("", encoding="utf-8")

    success = True
    failure_step: str | None = None

    clean = _clean_env()

    ok, _ = run_logged(
        [
            "meson",
            "setup",
            str(build_dir),
            str(repo),
            "--prefix=/usr",
            "--sysconfdir=/etc",
            "--localstatedir=/var",
            "--libdir=lib/x86_64-linux-gnu",
        ],
        log_file=log_file,
        env=clean,
    )
    if not ok:
        success = False
        failure_step = "meson setup"

    compile_output = ""
    if success:
        ok, compile_output = run_logged(
            ["meson", "compile", "-C", str(build_dir)],
            log_file=log_file,
            env=clean,
        )
        if not ok:
            success = False
            failure_step = "meson compile"

    if success:
        install_env = {**clean, "DESTDIR": str(stage_dir)}
        ok, _ = run_logged(
            ["meson", "install", "-C", str(build_dir)],
            log_file=log_file,
            env=install_env,
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

    # Run expert rules when compile failed and we have output to evaluate.
    # stage() compiles on the host, so use host include roots regardless of
    # whether a .orthos/chroot/ directory exists from prior convergence work.
    expert_verdicts: list[dict] = []
    if failure_step == "meson compile" and compile_output:
        verdicts = evaluate_compile_failure(compile_output, _stage_include_roots())
        expert_verdicts = [v.as_dict() for v in verdicts]

    if expert_verdicts:
        result["expert_verdicts"] = expert_verdicts
        # Translate verdicts into a structured pipeline strategy recommendation.
        # Currently only one verdict drives a strategy change; extend here as
        # new rules are added.
        strategy = _next_step_strategy(expert_verdicts)
        if strategy:
            result.update(strategy)

    write_json(orthos / _RESULT_FILE, result)
    return (0 if success else 1), result

