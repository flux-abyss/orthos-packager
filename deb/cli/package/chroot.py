"""Chroot convergence and staging helpers for the orthos package command."""

import shutil
from pathlib import Path

from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.convergence import ConvergenceResult, run_convergence_loop
from deb.discovery.runner import RunnerProtocol
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
                info(f"  {miss.miss_type}: {miss.name}")
                info(f"    from: {miss.raw_line}")
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
    meson_options: dict[str, str] | None = None,
) -> int:
    """Run Meson setup/compile/install inside the chroot for the staging phase.

    Mount layout (reuses setup_mounts with stage_build_dir as build_dir):
      /orthos/source  -> repo           (read-only source bind)
      /orthos/build   -> stage_build_dir (writable Meson build tree)
      /orthos/logs    -> logs_dir

    meson install uses DESTDIR=/orthos/build/destdir so the staged tree lands
    inside stage_build_dir/destdir on the host.  After mount teardown the CLI
    copies that tree to orthos/stage so inventory/classify can walk it.

    Returns 0 on success, 1 on any failure.
    """
    from deb.privileged.client import chroot_exec

    meson_option_flags = [
        f"-D{k}={v}" for k, v in sorted((meson_options or {}).items())
    ]
    stage_log = logs_dir / "package-chroot-stage.log"
    stage_log.write_text("", encoding="utf-8")

    # Remove any stale stage-result.json from a previous host-mode run so
    # inventory/classify cannot see an outdated failure record.
    stale_result = orthos / "stage-result.json"
    if stale_result.exists():
        stale_result.unlink()

    # Determine whether the stage build dir already contains a Meson build tree.
    # --wipe is only valid for an already-configured build directory; on a fresh
    # dir some Meson versions reject it.
    _coredata = stage_build_dir / "meson-private" / "coredata.dat"
    _already_configured = _coredata.exists()

    if not _already_configured:
        # Ensure a clean, empty staging build dir before mounting.
        if stage_build_dir.exists():
            shutil.rmtree(stage_build_dir)
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
    output = ""
    failure_step = ""

    try:
        # Build the meson setup command; include --wipe only when reconfiguring.
        if _already_configured:
            setup_cmd = [
                "meson", "setup", "--wipe",
                "/orthos/build",
                "/orthos/source",
                "--prefix=/usr",
                "--sysconfdir=/etc",
                "--localstatedir=/var",
                "--libdir=lib/x86_64-linux-gnu",
                *meson_option_flags,
            ]
        else:
            setup_cmd = [
                "meson", "setup",
                "/orthos/build",
                "/orthos/source",
                "--prefix=/usr",
                "--sysconfdir=/etc",
                "--localstatedir=/var",
                "--libdir=lib/x86_64-linux-gnu",
                *meson_option_flags,
            ]
        info("package: chroot stage - meson setup")
        ok, output = chroot_exec(env.root, setup_cmd)
        stage_log.write_text(output, encoding="utf-8")
        if not ok:
            failure_step = "meson setup"

        if ok:
            compile_cmd = ["meson", "compile", "-C", "/orthos/build"]
            info("package: chroot stage - meson compile")
            ok, output = chroot_exec(env.root, compile_cmd)
            with stage_log.open("a", encoding="utf-8") as fh:
                fh.write("\n" + output)
            if not ok:
                failure_step = "meson compile"

        if ok:
            # meson install needs DESTDIR; pass via bash -c to set env inline.
            install_cmd = [
                "bash", "-c",
                "DESTDIR=/orthos/build/destdir meson install -C /orthos/build",
            ]
            info("package: chroot stage - meson install")
            ok, output = chroot_exec(env.root, install_cmd)
            with stage_log.open("a", encoding="utf-8") as fh:
                fh.write("\n" + output)
            if not ok:
                failure_step = "meson install"

    finally:
        env.teardown_mounts()

    if not ok:
        error(f"package: chroot stage failed at: {failure_step}. see log: {stage_log}")
        return 1

    # Copy staged tree from stage_build_dir/destdir to orthos/stage.
    # Files are root-owned but world-readable; shutil.copytree can read them.
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

    # Write a fresh stage-result.json so inventory/classify see a successful
    # stage rather than any stale record from a previous host-mode run.
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
