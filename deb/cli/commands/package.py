"""package command handler and orchestration helpers."""

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from deb.backends.build_backend_debian import build as run_build
from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.runner import ChrootRunner, HostRunner, RunnerProtocol
from deb.paths import (
    orthos_dir,
    shared_chroot_dir,
    shared_convergence_build_dir,
    shared_stage_build_dir,
)
from deb.privileged import client as priv_client
from deb.privileged.launcher import PrivilegedHelperError
from deb.utils.fs import ensure_dir
from deb.utils.log import error, info
from deb.cli.package.build_source import (
    prepare_build_source,
    copy_generated_debian_to_build_source,
)
from deb.cli.package.artifacts import _install_built_debs
from deb.cli.package.chroot import _run_convergence_loop, _run_chroot_stage


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
    """Run scan->generate pipeline for package (no apply, no repo/debian requirement).

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

    *chroot_path* is forwarded to the build backend via 'meta["_chroot_path"]'
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
            runner = ChrootRunner(env, host_build_dir=convergence_build_dir)
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
