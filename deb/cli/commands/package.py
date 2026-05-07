"""package command handler and orchestration helpers."""

import argparse
import json
import time
import shutil
from pathlib import Path
from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.runner import ChrootRunner
from deb.paths import (
    chroot_target_name,
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
from deb.cli.package.report import write_package_report, print_verdict


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
    """Run the full packaging pipeline, write a report, and print a verdict."""
    _t0 = time.monotonic()
    repo_path = args.repo_path
    repo = Path(repo_path)
    orthos = orthos_dir(repo)

    rc = _cmd_package_inner(
        args, probe, cmd_scan, cmd_stage, cmd_inventory, cmd_classify, cmd_generate
    )

    elapsed = time.monotonic() - _t0
    status = "OK" if rc == 0 else "FAILED"

    try:
        from deb.discovery.miss_classifier import source_issue_diagnostic
        conv_data = {}
        try:
            import json as _json
            conv_data = _json.loads((orthos / "convergence-result.json").read_text())
        except Exception:
            pass
        source_issues = [
            source_issue_diagnostic(m.get("name", ""))
            for m in conv_data.get("unresolved_misses", [])
            if m.get("miss_type") == "source-issue"
        ]

        meta_data = {}
        try:
            meta_data = _json.loads((orthos / "package-meta.json").read_text())
        except Exception:
            pass

        gen_data = {}
        try:
            gen_data = _json.loads((orthos / "generate-result.json").read_text())
        except Exception:
            pass

        write_package_report(orthos, status, elapsed)

        artifacts_dir = orthos / "artifacts"
        artifact_paths = sorted(artifacts_dir.glob("*.deb")) if artifacts_dir.is_dir() else []
        total_bytes = sum(p.stat().st_size for p in artifact_paths if p.exists())

        print_verdict(
            status=status,
            project_name=meta_data.get("project_name") or repo.name,
            version=meta_data.get("version") or "",
            mode=conv_data.get("runner_mode") or "chroot",
            elapsed=elapsed,
            pkg_count=len(gen_data.get("binary_packages") or []),
            artifact_count=len(artifact_paths),
            total_artifact_bytes=total_bytes,
            orthos=orthos,
            source_issues=source_issues or None,
        )
    except Exception:
        # Report generation must never crash the package command.
        pass

    return rc


# pylint: disable=too-many-return-statements,too-many-locals
def _cmd_package_inner(
    args: argparse.Namespace,
    probe,
    cmd_scan,
    cmd_stage,
    cmd_inventory,
    cmd_classify,
    cmd_generate,
) -> int:
    """Run the full packaging pipeline (always chroot mode) and collect artifacts."""
    repo_path = args.repo_path
    repo = Path(repo_path)
    orthos = orthos_dir(repo)

    # Probe up-front so build_backend is known before convergence decisions.
    # Probe is cheap (reads meson.build or pyproject.toml, no network).
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    build_backend = meta.get("build_backend", "meson")

    from deb.cli.options import parse_meson_options
    _meson_options = parse_meson_options(getattr(args, "meson_options", []))

    chroot_name = chroot_target_name(args.chroot_suite, args.target_repo_set)
    chroot_root = shared_chroot_dir(chroot_name)
    logs_dir = orthos / "logs"
    convergence_build_dir = shared_convergence_build_dir(chroot_name, repo.name)
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

    # Convergence is Meson-only: it runs meson setup iteratively to discover
    # missing build deps.  Python projects skip it and install stage_deps()
    # directly inside _run_chroot_stage.
    if build_backend == "meson":
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

        info("package: convergence run in chroot target environment")
    else:
        info(f"package: skipping Meson convergence (build_backend={build_backend!r})")

    # Stage inside the chroot using a separate build dir so convergence state
    # is never clobbered.
    stage_build_dir = shared_stage_build_dir(chroot_name, repo.name)
    ensure_dir(stage_build_dir)

    rc = _run_chroot_stage(
        env,
        repo,
        orthos,
        stage_build_dir,
        logs_dir,
        meta=meta,
        meson_options=_meson_options or None,
    )
    if rc != 0:
        return rc

    # Python: staging succeeded.  Debian generation/build is not yet
    # implemented for python-pyproject.  Exit with a clear error message.
    if build_backend != "meson":
        info("package: python chroot staging complete")
        error("package: Debian generation/build for Python projects is not implemented yet")
        error("package: Milestone D will add Python debian/rules and Build-Depends support")
        return 1

    _chroot_path = str(chroot_root)
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
        skip_stage=True,
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

    if not args.install_host:
        info("package complete ✔")
        return 0

    info("package: installing artifacts on host (--install-host)")
    rc = _install_built_debs(debs)
    if rc != 0:
        return rc

    info("package complete ✔")
    return 0


def cmd_reset_chroot(
    repo_path: str,
    suite: str = "trixie",
    repo_set: str | None = None,
) -> int:
    """Tear down mounts and remove the shared chroot and convergence work.

    Both trees may be root-owned (managed by orthos-priv).  This command
    removes them so the user never needs sudo for project workspace cleanup.
    """
    repo = Path(repo_path)
    target_name = chroot_target_name(suite, repo_set)
    chroot_root = shared_chroot_dir(target_name)
    info(f"reset-chroot: suite: {suite}")
    info(f"reset-chroot: target repo: {repo_set or 'native'}")
    info(f"reset-chroot: target name: {target_name}")
    info(f"reset-chroot: chroot target: {chroot_root}")
    try:
        priv_client.reset_chroot(chroot_root)
    except PrivilegedHelperError as exc:
        error(f"reset-chroot: chroot removal failed: {exc}")
        return 1
    info(f"reset-chroot: chroot done: {chroot_root}")

    # Also destroy the per-repo convergence work dir (root-owned Meson output).
    conv_work = shared_convergence_build_dir(target_name, repo.name)
    info(f"reset-chroot: convergence work target: {conv_work}")
    try:
        priv_client.destroy_convergence_work(conv_work)
    except PrivilegedHelperError as exc:
        error(f"reset-chroot: convergence work removal failed: {exc}")
        return 1
    info(f"reset-chroot: convergence work done: {conv_work}")
    return 0
