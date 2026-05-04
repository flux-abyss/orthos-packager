"""package command handler and orchestration helpers."""

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from deb.backends.build_backend_debian import build as run_build
from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.convergence import ConvergenceResult, run_convergence_loop
from deb.discovery.runner import ChrootRunner, HostRunner, RunnerProtocol
from deb.paths import (
    orthos_dir,
    shared_chroot_dir,
    shared_convergence_build_dir,
    shared_stage_build_dir,
)
from deb.privileged import client as priv_client
from deb.privileged.launcher import PrivilegedHelperError
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import error, info


# Directories excluded from the isolated package source copy.
_BUILD_SRC_EXCLUDE = {".git", ".orthos", "build", "dist", "__pycache__", "debian"}


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


def _partition_debs(debs: list[str]) -> tuple[list[str], list[str]]:
    """Return (main_debs, dbgsym_debs) partitioned from *debs*."""
    main_debs = [d for d in debs if "-dbgsym_" not in d]
    dbgsym_debs = [d for d in debs if "-dbgsym_" in d]
    return main_debs, dbgsym_debs


def _run_package_prebuild_pipeline(
    repo_path: str,
    probe,
    cmd_scan,
    cmd_stage,
    cmd_inventory,
    cmd_classify,
    cmd_generate,
    chroot_path: str | None = None,
    meson_options: dict[str, str] | None = None,
    skip_stage: bool = False,
) -> int:
    """Run scan→generate pipeline for package (no apply, no repo/debian requirement).

    When *skip_stage* is True, the cmd_stage step is omitted (used in chroot
    mode where staging already ran inside the chroot via _run_chroot_stage).
    """
    scan_rc = cmd_scan(repo_path)
    if scan_rc != 0:
        return scan_rc

    if not skip_stage:
        stage_rc = cmd_stage(repo_path, meson_options=meson_options)
        if stage_rc != 0:
            return stage_rc

    for step in (cmd_inventory, cmd_classify):
        rc = step(repo_path)
        if rc != 0:
            return rc

    rc = cmd_generate(repo_path, chroot_path=chroot_path, meson_options=meson_options)
    if rc != 0:
        return rc

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


def prepare_build_source(repo_path: Path, orthos_path: Path) -> Path:
    """Create an isolated copy of *repo_path* under *orthos_path*/build-src/.

    Any previous build-src is removed before recreation so the copy is always
    fresh.  Only .git, .orthos, build, dist, __pycache__, and debian are
    excluded; all other source files are preserved verbatim.

    Returns the path to the new build-src directory.
    """
    build_src = orthos_path / "build-src"
    if build_src.exists():
        shutil.rmtree(build_src)

    def _ignore(src: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _BUILD_SRC_EXCLUDE}

    shutil.copytree(repo_path, build_src, ignore=_ignore, dirs_exist_ok=False)
    return build_src


def copy_generated_debian_to_build_source(
    generated_debian: Path, build_src: Path
) -> None:
    """Copy *generated_debian* into *build_src*/debian/.

    Any previous build_src/debian is removed first so the injection is clean.
    """
    target = build_src / "debian"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(generated_debian, target, dirs_exist_ok=False)


def _run_package_build_step(
    build_src: Path,
    original_orthos: Path,
    probe,
    chroot_path: str | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Build from the isolated *build_src* copy and log the outcome.

    *build_src* is passed as repo_path to run_build so dpkg-buildpackage
    runs inside the isolated copy.  The orthos workspace used for result
    files and artifact storage is still the *original_orthos* directory so
    the caller can find build-result.json in the usual place.

    *chroot_path* is forwarded to the build backend via 'meta['_chroot_path']'
    so that artifact validation uses the chroot's apt database rather than the
    host's.  Pass 'None' to fall back to host-scoped validation (with a
    contamination warning).

    Returns (rc, result or None).
    """
    try:
        # Probe the isolated copy; it has all source files + injected debian/.
        meta = probe(str(build_src))
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1, None

    # Override the orthos workspace so results land in the original repo's
    # .orthos dir, not inside build-src's (nonexistent) .orthos.
    meta["_orthos_override"] = str(original_orthos)
    # Forward the target chroot path so artifact validation uses the target
    # Debian environment (ChrootAptOracle) rather than the host's apt.
    if chroot_path:
        meta["_chroot_path"] = chroot_path

    try:
        rc, result = run_build(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1, None

    info(f"repo:    {result['repo_path']}")
    info(f"debian:  {result['target_debian_dir']}")
    info(f"log:     {result['log_file']}")

    if result["success"]:
        info("result:  success")
        info(f"artifacts: {len(result['artifacts'])}")
        for p in result["artifacts"]:
            info(f"  {p}")
    else:
        failure_step = result.get("failure_step") or "unknown"
        error(f"build failed at: {failure_step}")
        error(f"see log: {result['log_file']}")

    return rc, result


def _run_build_step(repo_path: str, probe) -> tuple[int, dict[str, Any] | None]:
    """Probe, build, and log the build outcome. Returns (rc, result or None)."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1, None

    try:
        rc, result = run_build(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1, None

    info(f"repo:    {result['repo_path']}")
    info(f"debian:  {result['target_debian_dir']}")
    info(f"log:     {result['log_file']}")

    if result["success"]:
        info("result:  success")
        info(f"artifacts: {len(result['artifacts'])}")
        for p in result["artifacts"]:
            info(f"  {p}")
    else:
        failure_step = result.get("failure_step") or "unknown"
        error(f"build failed at: {failure_step}")
        error(f"see log: {result['log_file']}")

    return rc, result


def _install_built_debs(debs: list[str]) -> int:
    """Install partitioned .deb artifacts via dpkg with apt -f fallback."""
    main_debs, dbgsym_debs = _partition_debs(debs)

    if main_debs:
        info(f"main packages:   {', '.join(main_debs)}")
    if dbgsym_debs:
        info(f"dbgsym packages: {', '.join(dbgsym_debs)}")
    info("install order: main first, dbgsym last")

    if main_debs:
        rc = subprocess.call(["sudo", "dpkg", "-i", *main_debs])
        if rc != 0:
            info(
                "dpkg reported issues on main packages, running apt -f install..."
            )
            rc = subprocess.call(["sudo", "apt", "-f", "install", "-y"])
            if rc != 0:
                error("apt failed to resolve dependencies for main packages")
                return rc

    if dbgsym_debs:
        rc = subprocess.call(["sudo", "dpkg", "-i", *dbgsym_debs])
        if rc != 0:
            info(
                "dpkg reported issues on dbgsym packages, running apt -f install..."
            )
            rc = subprocess.call(["sudo", "apt", "-f", "install", "-y"])
            if rc != 0:
                error("apt failed to resolve dependencies for dbgsym packages")
                return rc

    return 0


# pylint: disable=too-many-return-statements,too-many-locals
def cmd_package(
    args: argparse.Namespace,
    probe,
    cmd_scan,
    cmd_stage,
    cmd_inventory,
    cmd_classify,
    cmd_generate,
) -> int:
    """Run the full packaging pipeline and collect artifacts."""
    repo_path = args.repo_path
    repo = Path(repo_path)
    orthos = orthos_dir(repo)

    from deb.cli.options import parse_meson_options
    _meson_options = parse_meson_options(getattr(args, "meson_options", []))

    if args.host:
        # Pre-isolation host mode: explicit opt-in.
        info("convergence: mode = host (pre-isolation; --host flag set)")
        runner: RunnerProtocol = HostRunner()
        rc = _run_convergence_loop(repo_path, runner, meson_options=_meson_options or None)
    else:
        # Isolated chroot mode: default authoritative path.
        chroot_dir_name = f"{args.chroot_suite}-{args.target_repo_set}"
        chroot_root = shared_chroot_dir(chroot_dir_name)
        logs_dir = orthos / "logs"
        convergence_build_dir = shared_convergence_build_dir(
            chroot_dir_name, repo.name
        )
        ensure_dir(logs_dir)
        ensure_dir(convergence_build_dir)

        chroot_log = logs_dir / "chroot-setup.log"
        env = ChrootEnv(chroot_root)
        try:
            env.ensure_ready(
                suite=args.chroot_suite,
                repo_set=args.target_repo_set,
                refresh=args.refresh_chroot,
                log_file=chroot_log,
            )
        except ChrootEnvError as exc:
            error(f"convergence: chroot setup failed: {exc}")
            return 1

        try:
            env.setup_mounts(
                source_repo=repo,
                build_dir=convergence_build_dir,
                logs_dir=logs_dir,
            )
        except ChrootEnvError as exc:
            error(f"convergence: mount setup failed: {exc}")
            env.teardown_mounts()
            return 1

        try:
            runner = ChrootRunner(env)
            info(f"convergence: mode = chroot ({chroot_root})")
            rc = _run_convergence_loop(repo_path, runner, meson_options=_meson_options or None)
        finally:
            # Primary cleanup guarantee: always teardown mounts.
            env.teardown_mounts()

        if rc != 0:
            return rc

        info("package: convergence and staging run in chroot target environment")

        # Stage inside the chroot using a separate build dir to avoid
        # clobbering the convergence build state.
        chroot_dir_name_stage = f"{args.chroot_suite}-{args.target_repo_set}"
        stage_build_dir = shared_stage_build_dir(chroot_dir_name_stage, repo.name)
        ensure_dir(stage_build_dir)

        rc = _run_chroot_stage(
            env,
            repo,
            orthos,
            stage_build_dir,
            logs_dir,
            meson_options=_meson_options or None,
        )
        if rc != 0:
            return rc

    if rc != 0:
        return rc

    # scan → (stage already done in chroot if not host) → inventory → classify → generate
    chroot_dir_name = f"{args.chroot_suite}-{args.target_repo_set}" if hasattr(args, "target_repo_set") else args.chroot_suite
    _chroot_path = str(shared_chroot_dir(chroot_dir_name)) if not args.host else None
    rc = _run_package_prebuild_pipeline(
        repo_path,
        probe,
        cmd_scan,
        cmd_stage,
        cmd_inventory,
        cmd_classify,
        cmd_generate,
        chroot_path=_chroot_path,
        meson_options=_meson_options if _meson_options else None,
        skip_stage=not args.host,
    )
    if rc != 0:
        return rc

    # Create an isolated source copy and inject generated debian/ into it.
    generated_debian = orthos / "debian"
    if not generated_debian.is_dir():
        error(f"package: generated debian/ not found: {generated_debian}")
        return 1

    build_src = prepare_build_source(repo, orthos)
    info(f"package: building from isolated source copy: {build_src}")
    copy_generated_debian_to_build_source(generated_debian, build_src)

    # Build from the isolated copy; results go to the original orthos dir.
    # Forward the chroot path so artifact validation uses the target oracle.
    if not args.host:
        from deb.privileged.client import chroot_exec
        from deb.resolution.oracle import make_oracle
        from deb.resolution.debian import validate_built_debs

        artifacts_dir = orthos / "artifacts"
        ensure_dir(artifacts_dir)
        for p in artifacts_dir.glob("*.deb"):
            p.unlink()

        # Load expected package names before building so we can filter the
        # artifact copy.  Compute the full allowed set (base names + dbgsym).
        gen_result_file = orthos / "generate-result.json"
        generated_pkg_names: frozenset[str] = frozenset()
        if gen_result_file.exists():
            try:
                gen_result = json.loads(gen_result_file.read_text(encoding="utf-8"))
                generated_pkg_names = frozenset(gen_result.get("binary_packages", []))
            except Exception:
                pass

        _allowed_pkg_names: frozenset[str] = frozenset(
            {n for n in generated_pkg_names}
            | {f"{n}-dbgsym" for n in generated_pkg_names}
        )

        try:
            env.setup_mounts(
                source_repo=repo,
                build_dir=convergence_build_dir,
                logs_dir=logs_dir,
                build_src=build_src,
            )

            info("package: running dpkg-buildpackage inside chroot")

            ok, output = chroot_exec(
                env.root,
                ["bash", "-c", "cd /orthos/build-src && apt-get update && apt-get build-dep -y ."]
            )
            build_log = logs_dir / "package-chroot-build.log"
            build_log.write_text(output, encoding="utf-8")

            if not ok:
                error(f"package: chroot build-dep failed. see log: {build_log}")
                return 1

            ok, output2 = chroot_exec(
                env.root,
                ["bash", "-c", "cd /orthos/build-src && dpkg-buildpackage -us -uc -b"]
            )
            with open(build_log, "a", encoding="utf-8") as f:
                f.write("\n" + output2)

            if not ok:
                error(f"package: chroot build failed. see log: {build_log}")
                return 1

            chroot_orthos = env.root / "orthos"
            for p in chroot_orthos.glob("*.deb"):
                # Derive the binary package name: "pkg_ver_arch.deb" -> "pkg"
                pkg_name = p.name.split("_")[0]
                if _allowed_pkg_names and pkg_name not in _allowed_pkg_names:
                    info(f"package: skipping unrelated artifact: {p.name}")
                    continue
                dst = artifacts_dir / p.name
                shutil.copy(str(p), str(dst))

        finally:
            env.teardown_mounts()

        debs = sorted(str(p) for p in artifacts_dir.glob("*.deb"))
        if not debs:
            _expected = ", ".join(sorted(_allowed_pkg_names)) or "(unknown)"
            error(
                f"package: no matching .deb artifacts found in {chroot_orthos}. "
                f"expected packages: {_expected}"
            )
            return 1

        oracle = make_oracle(env.root)
        try:
            validate_built_debs(debs, generated_pkg_names, oracle)
        except RuntimeError as exc:
            error(str(exc))
            return 1

        info("package: build complete (chroot)")
        for p in debs:
            info(f"  artifact: {p}")

    else:
        rc, result = _run_package_build_step(build_src, orthos, probe, chroot_path=None)
        if result is None or not result["success"]:
            return rc

        debs = sorted(p for p in result["artifacts"] if p.endswith(".deb"))
        if not debs:
            error("no .deb artifacts found")
            return 1

        info("package: build complete (host)")
        for p in debs:
            info(f"  artifact: {p}")

    if not args.install_host:
        info("package: host install skipped (build-only mode)")
        info("package: use --install-host to install artifacts on this system")
        info("package complete ✔")
        return 0

    info("package: installing artifacts on host (--install-host)")
    rc = _install_built_debs(debs)
    if rc != 0:
        return rc

    info("package complete ✔")
    return 0


def cmd_reset_chroot(repo_path: str, suite: str = "trixie") -> int:
    """Tear down mounts and remove the shared chroot and convergence work.

    Targets:
      - shared chroot:        .orthos/chroots/<suite>-<arch>/
      - convergence work dir: .orthos/chroot-work/<suite>-<arch>/<repo>/

    Both trees may be root-owned (managed by orthos-priv).  This command
    removes them so the user never needs sudo for project workspace cleanup.
    """
    repo = Path(repo_path)
    chroot_root = shared_chroot_dir(suite)
    info(f"reset-chroot: suite: {suite}")
    info(f"reset-chroot: chroot target: {chroot_root}")
    try:
        priv_client.reset_chroot(chroot_root)
    except PrivilegedHelperError as exc:
        error(f"reset-chroot: chroot removal failed: {exc}")
        return 1
    info(f"reset-chroot: chroot done: {chroot_root}")

    # Also destroy the per-repo convergence work dir (root-owned Meson output).
    conv_work = shared_convergence_build_dir(suite, repo.name)
    info(f"reset-chroot: convergence work target: {conv_work}")
    try:
        priv_client.destroy_convergence_work(conv_work)
    except PrivilegedHelperError as exc:
        error(f"reset-chroot: convergence work removal failed: {exc}")
        return 1
    info(f"reset-chroot: convergence work done: {conv_work}")
    return 0
