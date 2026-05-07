"""Chroot convergence and staging helpers for the orthos package command."""

import shutil
from pathlib import Path

from deb.backends.meson import _CARGO_ENV  # noqa: F401 — re-exported for tests
from deb.backends.registry import get_backend
from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.convergence import (
    ConvergenceResult,
    run_convergence_loop,
)
from deb.discovery.miss_classifier import source_issue_diagnostic
from deb.discovery.runner import ChrootRunner, RunnerProtocol
from deb.privileged.client import PrivilegedHelperError, destroy_convergence_work
from deb.utils.fs import write_json
from deb.utils.log import error, info


def _run_convergence_loop(
    repo_path: str,
    runner: RunnerProtocol,
    meson_options: dict[str, str] | None = None,
) -> int:
    """Run the convergence scaffold via *runner* and log the outcome.

    Returns:
      0 - converged successfully or stalled (nonfatal; stage step handles it)
      1 - apt install failed inside the loop (fatal; package must stop)
    """
    repo = Path(repo_path)
    result: ConvergenceResult = run_convergence_loop(
        repo, runner=runner, meson_options=meson_options
    )

    info(f"convergence: {result.passes} pass(es) completed "
         f"(mode={result.runner_mode}, scope={result.isolation_scope})")

    for entry in result.provenance:
        info(f"convergence: {entry.package} "
             f"[{entry.miss_type}] pass {entry.pass_number}")

    if result.large_batch_warnings:
        for w in result.large_batch_warnings:
            info(f"convergence: WARNING - {w}")

    # Fatal: apt install failed inside the convergence loop.
    if result.install_failed:
        error("convergence: apt install failed - aborting package")
        return 1

    if result.success:
        info("convergence: meson setup converged - "
             "setup-time dependencies satisfied")
        return 0

    if result.stalled:
        if result.stall_reason == "unresolved":
            info(f"convergence: stalled - "
                 f"{len(result.unresolved_misses)} miss(es) unresolvable:")
            for miss in result.unresolved_misses:
                if miss.miss_type == "source-issue":
                    info(f"  source-issue: {source_issue_diagnostic(miss.name)}")
                else:
                    info(f"  {miss.miss_type}: {miss.name}")
                info(f"    from: {miss.raw_line}")

            if all(m.miss_type == "source-issue" for m in result.unresolved_misses):
                error("convergence: fatal source-side issues detected - aborting")
                return 1
        else:
            info("convergence: stalled - no new packages to install; "
                 "proceeding to stage")
    else:
        info("convergence: max passes exhausted without setup success; "
             "proceeding to stage")

    # Nonfatal stall - let the stage step fail explicitly so the
    # human maintainer sees a concrete error.
    return 0


def _run_chroot_stage(
    env: ChrootEnv,
    repo: Path,
    orthos: Path,
    stage_build_dir: Path,
    logs_dir: Path,
    meta: dict,
    meson_options: dict[str, str] | None = None,
) -> int:
    """Run backend-specific setup/build/install inside the chroot for staging.

    Dispatches to the detected backend's stage_chroot() method, which owns
    all build-system-specific commands.  Mount lifecycle, DESTDIR copy, and
    stage-result.json writing are handled here regardless of backend.

    Mount layout:
      /orthos/source  -> repo           (read-only source bind)
      /orthos/build   -> stage_build_dir (writable build tree)
      /orthos/logs    -> logs_dir

    Returns 0 on success, 1 on any failure.
    """
    from deb.privileged.client import chroot_exec  # noqa: PLC0415

    backend_name = meta.get("build_backend", "meson")
    backend = get_backend(backend_name)

    stage_log = logs_dir / "package-chroot-stage.log"
    stage_log.write_text("", encoding="utf-8")

    # Remove any stale stage-result.json from a previous run.
    stale_result = orthos / "stage-result.json"
    if stale_result.exists():
        stale_result.unlink()

    # Install stage-time deps (idempotent; Meson returns []).
    # Done before mounting so apt runs against the unmounted chroot state.
    stage_pkg_list = backend.stage_deps()
    if stage_pkg_list:
        info(f"package: installing stage deps for {backend_name}: {stage_pkg_list}")
        _runner = ChrootRunner(env)
        missing = [p for p in stage_pkg_list if not _runner.is_pkg_installed(p)]
        if missing:
            rc = _runner.apt_install(missing)
            if rc != 0:
                error(f"package: failed to install stage deps: {missing}")
                return 1

    # Ensure a clean, empty staging build dir before mounting.
    # May be root-owned from a previous chroot run; use the privileged helper.
    if stage_build_dir.exists():
        try:
            destroy_convergence_work(stage_build_dir)
        except PrivilegedHelperError as exc:
            error(f"package: failed to clean stage build dir: {exc}")
            return 1
    stage_build_dir.mkdir(parents=True, exist_ok=True)

    try:
        env.setup_mounts(
            source_repo=repo,
            build_dir=stage_build_dir,
            logs_dir=logs_dir,
        )
    except ChrootEnvError as exc:
        error(f"package: chroot stage mount failed: {exc}")
        return 1

    ok = True
    failure_step = ""

    try:
        ok, failure_step = backend.stage_chroot(
            meta=meta,
            chroot_exec_fn=chroot_exec,
            chroot_root=env.root,
            source_path="/orthos/source",
            build_path="/orthos/build",
            destdir_path="/orthos/build/destdir",
            log_file=stage_log,
        )
    finally:
        env.teardown_mounts()

    if not ok:
        error(f"package: chroot stage failed at: {failure_step}. see log: {stage_log}")
        return 1

    # Copy staged tree from stage_build_dir/destdir to orthos/stage.
    # Chroot-created files are expected to be readable from the host; copytree
    # recreates the staged tree under the user-owned project workspace.
    destdir_host = stage_build_dir / "destdir"
    stage_target = orthos / "stage"

    if not destdir_host.exists():
        error(f"package: chroot stage produced no DESTDIR at {destdir_host}")
        return 1

    if stage_target.exists():
        shutil.rmtree(stage_target)

    try:
        shutil.copytree(str(destdir_host), str(stage_target), symlinks=True)
    except OSError as exc:
        error(f"package: failed to copy staged tree to {stage_target}: {exc}")
        return 1

    info(f"package: chroot stage complete - staged tree at {stage_target}")

    # Write a fresh stage-result.json so inventory/classify see the current
    # chroot staging result rather than any stale record from a previous run.
    write_json(orthos / "stage-result.json", {
        "build_dir": str(stage_build_dir),
        "log_file": str(stage_log),
        "project_name": repo.name,
        "repo_path": str(repo),
        "stage_dir": str(stage_target),
        "success": True,
        "version": "",
    })

    return 0
