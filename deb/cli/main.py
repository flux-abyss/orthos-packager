"""orthos-packager – entry point."""

import argparse
import shutil
import sys
import subprocess
from pathlib import Path
from typing import Any

from deb.analyze import analyze as run_analyze
from deb.apply_debian import apply as run_apply
from deb.backends.build_backend_debian import build as run_build
from deb.backends.build_backend_meson import stage as meson_stage
from deb.classifier.artifact_classifier import classify as run_classify
from deb.core.repo_probe import probe
from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.discovery.convergence import ConvergenceResult, run_convergence_loop
from deb.discovery.runner import ChrootRunner, HostRunner, RunnerProtocol
from deb.generator.debian_generator import generate as run_generate
from deb.inventory.install_inventory import build_inventory
from deb.privileged import client as priv_client
from deb.privileged.launcher import PrivilegedHelperError
from deb.suggest import suggest as run_suggest
from deb.paths import orthos_dir, shared_chroot_dir, shared_convergence_build_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import error, info

_META_FILE = "package-meta.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orthos-packager",
        description="Deterministic Debian packager for Meson projects.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    scan = sub.add_parser("scan",
                          help="Probe a repository and emit package metadata.")
    scan.add_argument("repo_path",
                      metavar="PATH",
                      help="Local path to the repository.")

    stage = sub.add_parser("stage", help="Build and stage a Meson repository.")
    stage.add_argument("repo_path",
                       metavar="PATH",
                       help="Local path to the repository.")

    inventory = sub.add_parser("inventory",
                               help="Inventory the staged install tree.")
    inventory.add_argument("repo_path",
                           metavar="PATH",
                           help="Local path to the repository.")

    classify = sub.add_parser("classify",
                              help="Group inventory into package buckets.")
    classify.add_argument("repo_path",
                          metavar="PATH",
                          help="Local path to the repository.")

    generate = sub.add_parser(
        "generate", help="Generate a debian/ skeleton from the package plan.")
    generate.add_argument("repo_path",
                          metavar="PATH",
                          help="Local path to the repository.")

    apply_p = sub.add_parser(
        "apply", help="Materialize generated debian/ into the source repo.")
    apply_p.add_argument("repo_path",
                         metavar="PATH",
                         help="Local path to the repository.")
    apply_p.add_argument("--force",
                         action="store_true",
                         help="Overwrite existing repo/debian if present.")

    build_p = sub.add_parser("build",
                             help="Build Debian packages using repo/debian.")
    build_p.add_argument("repo_path",
                         metavar="PATH",
                         help="Local path to the repository.")

    analyze_p = sub.add_parser("analyze",
                               help="Analyze the last build result and log.")
    analyze_p.add_argument("repo_path",
                           metavar="PATH",
                           help="Local path to the repository.")

    suggest_p = sub.add_parser("suggest",
                               help="Suggest a fix for the last build failure.")
    suggest_p.add_argument("repo_path",
                           metavar="PATH",
                           help="Local path to the repository.")

    smoke = sub.add_parser(
        "smoke",
        help=(
            "Run convergence + full build pipeline. "
            "Build-only by default; use --install-host to install artifacts on this system."
        ),
    )
    smoke.add_argument(
        "repo_path",
        metavar="PATH",
        help="Local path to the repository.",
    )
    mod_group = smoke.add_mutually_exclusive_group()
    mod_group.add_argument(
        "--host",
        action="store_true",
        default=False,
        help=(
            "Run convergence directly on the host (pre-isolation mode). "
            "Default (without this flag) is isolated chroot mode."
        ),
    )
    smoke.add_argument(
        "--refresh-chroot",
        action="store_true",
        default=False,
        help="Delete and recreate the chroot before running.",
    )
    smoke.add_argument(
        "--chroot-suite",
        metavar="SUITE",
        default="trixie",
        help="Debian suite for chroot creation (default: trixie).",
    )
    smoke.add_argument(
        "--target-repo-set",
        choices=["debian", "bodhi"],
        default="debian",
        help="Target package universe for chroot creation (default: debian).",
    )
    smoke.add_argument(
        "--install-host",
        action="store_true",
        default=False,
        help=(
            "Install the built packages onto this system after a successful build. "
            "Default (without this flag) is build-only: artifacts are produced and "
            "printed but NOT installed. Use this flag only when you explicitly want "
            "to install the packages on the current host."
        ),
    )

    reset_chroot_p = sub.add_parser(
        "reset-chroot",
        help="Safely tear down mounts and remove the shared chroot for a suite.",
    )
    reset_chroot_p.add_argument(
        "repo_path",
        metavar="PATH",
        help="Repository path (used to locate the Orthos workspace; chroot is shared).",
    )
    reset_chroot_p.add_argument(
        "--chroot-suite",
        metavar="SUITE",
        default="trixie",
        help="Debian suite whose shared chroot should be reset (default: trixie).",
    )

    return parser


def _cmd_scan(repo_path: str) -> int:
    """Run the scan command and write package metadata JSON."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    out_dir = orthos_dir(Path(meta["repo_path"]))
    ensure_dir(out_dir)
    out_file = out_dir / _META_FILE
    write_json(out_file, meta)

    name = meta["project_name"] or "(unknown)"
    version = meta["version"] or "(unknown)"
    debian = "yes" if meta["debian_dir"] else "no"

    info(f"repo:    {meta['repo_path']}")
    info(f"project: {name}  version: {version}")
    info(f"debian/: {debian}")

    dc = meta.get("distro_candidate")
    if dc:
        info(f"distro:  {dc['package']} = {dc['candidate_version']}")
        # Extract major.minor from candidate version as the recommended anchor.
        parts = dc["candidate_version"].split(".")
        if len(parts) >= 2:
            anchor = f"{parts[0]}.{parts[1]}"
            info(f"anchor:  start from upstream ~{anchor} before compatibility guessing")
    else:
        info("distro:  (package not found in configured apt sources)")

    info(f"wrote:   {out_file}")

    return 0


def _cmd_stage(repo_path: str) -> int:
    """Run the Meson staging pipeline for a repository."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"staging: {meta['repo_path']}")
    info("running meson setup …")

    rc, result = meson_stage(meta)

    if rc == 0:
        info(f"project: {result['project_name'] or '(unknown)'}  "
             f"version: {result['version'] or '(unknown)'}")
        info(f"stage:   {result['stage_dir']}")
        info(f"log:     {result['log_file']}")
        info("result:  success")
    else:
        step = result.get("failure_step", "unknown step")
        error(f"staging failed at: {step}")
        error(f"see log: {result['log_file']}")

        for verdict in result.get("expert_verdicts", []):
            info(f"expert:  [{verdict['rule_id']}] "
                 f"confidence={verdict['confidence']:.0%}")
            info(f"         {verdict['summary']}")
            info(f"         action: {verdict['suggested_action']}")

        if result.get("next_mode") == "compatibility_search":
            info("next:    compatibility search mode")
            info("prefer:  an older release/tag before more dependency resolution")
            sp = result.get("symbol_provider")
            if sp:
                info(f"symbol:  {sp['symbol']}")
                info(f"header:  {sp['header']}")
                info(f"inferred: {sp['package']}")
            tvi = result.get("target_version_info")
            if tvi:
                info(f"target:  {tvi['package']} = {tvi['package_version'] or '(not installed)'}")
                info(f"pc:      {tvi['pkgconfig_module']} = {tvi['pkgconfig_version'] or '(not found)'}")

                # Extract major.minor version hint from pkgconfig_version.
                pc_ver: str = tvi.get("pkgconfig_version") or ""
                parts = pc_ver.split(".")
                if len(parts) >= 2:
                    major_minor = f"{parts[0]}.{parts[1]}"
                    info(f"hint:    upstream versions near {major_minor}.x are likely compatible")

                    # Suggest local tags that match the major.minor prefix.
                    # best-effort: silently skips if git is unavailable or repo
                    # has no tags.
                    try:
                        tag_result = subprocess.run(
                            ["git", "-C", repo_path, "tag"],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5,
                        )
                        if tag_result.returncode == 0:
                            matching = [
                                t for t in tag_result.stdout.splitlines()
                                if t.startswith(major_minor)
                            ]
                            if matching:
                                info("suggest:")
                                for tag in sorted(matching, reverse=True)[:5]:
                                    info(f"  {tag}")
                            else:
                                info(f"suggest: no local tags found matching {major_minor}.*")
                    except (OSError, subprocess.TimeoutExpired):
                        pass


    return rc


def _cmd_inventory(repo_path: str) -> int:
    """Inventory the staged install tree and write install-inventory.json."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        rc, result = build_inventory(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"stage:   {result['stage_dir']}")
    info(f"files:   {result['total_files']}")

    for kind, count in sorted(result["counts_by_kind"].items()):
        info(f"  {kind:<12} {count}")

    info(f"wrote:   {result['inventory_file']}")

    return rc


def _cmd_classify(repo_path: str) -> int:
    """Group inventory entries into package buckets and write package-plan.json."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        rc, result = run_classify(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"inv:     {result['inventory_file']}")
    info(f"files:   {result['total_files']}")

    for bucket in result["package_buckets"]:
        info(f"  {bucket['name']:<10} {bucket['file_count']}")

    info(f"wrote:   {result['plan_file']}")
    return rc


def _cmd_generate(repo_path: str, chroot_path: str | None = None) -> int:
    """Generate a debian/ skeleton from the package plan."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    if chroot_path is not None:
        meta["_chroot_path"] = chroot_path

    try:
        rc, result = run_generate(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"plan:    {result['plan_file']}")
    info(f"debian:  {result['debian_dir']}")
    info(f"files:   {len(result['generated_files'])}")
    for pkg in result["binary_packages"]:
        info(f"  {pkg}")
    info(f"wrote:   {result['debian_dir']}")

    _print_validation_summary(result)
    return rc


def _print_validation_summary(result: dict[str, Any]) -> None:
    """Print a concise inter-package and -dev validation summary."""
    iv = result.get("inter_pkg_validation")
    if iv:
        present = iv.get("present_primary_depends", [])
        missing = iv.get("missing_primary_depends", [])
        present_str = ", ".join(
            p.split("-", 1)[-1] for p in present) or "none"
        missing_str = ", ".join(
            p.split("-", 1)[-1] for p in missing) or "none"
        info("inter-package validation:")
        info(f"  primary depends ok: {present_str}")
        info(f"  missing:            {missing_str}")

    dv = result.get("dev_pkg_validation", [])
    if dv:
        info("dev package validation:")
        for rec in dv:
            lockstep = "ok" if rec["has_main_lockstep_dep"] else "MISSING"
            leakage = "yes (WARN)" if rec["has_shlibs_dep"] else "no"
            info(f"  {rec['package']} lockstep dep: {lockstep}")
            info(f"  {rec['package']} shlibs leakage: {leakage}")


def _cmd_apply(repo_path: str, force: bool = False) -> int:
    """Materialize generated debian/ from the orthos workspace into the repo."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result = run_apply(meta, force=force)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1
    except FileExistsError as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"source:  {result['source_debian_dir']}")
    info(f"target:  {result['target_debian_dir']}")
    if result["overwritten"]:
        info("overwritten: yes")
    info("result:  applied")
    return 0


def _cmd_build(repo_path: str) -> int:
    """Run dpkg-buildpackage using repo/debian."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        rc, result = run_build(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

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

    return rc


def _run_convergence_loop(repo_path: str, runner: RunnerProtocol) -> int:
    """Run the convergence scaffold via *runner* and log the outcome.

    Returns:
      0 - converged successfully or stalled (nonfatal; stage step handles it)
      1 - apt install failed inside the loop (fatal; smoke must stop)
    """
    repo = Path(repo_path)
    result: ConvergenceResult = run_convergence_loop(repo, runner=runner)

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
        error("convergence: apt install failed - aborting smoke")
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


def _run_smoke_prebuild_pipeline(repo_path: str, chroot_path: str | None = None) -> int:
    """Run scan→generate pipeline for smoke (no apply, no repo/debian requirement)."""
    for step in (
            _cmd_scan,
            _cmd_stage,
            _cmd_inventory,
            _cmd_classify,
    ):
        rc = step(repo_path)
        if rc != 0:
            return rc
            
    rc = _cmd_generate(repo_path, chroot_path=chroot_path)
    if rc != 0:
        return rc
        
    return 0


# Directories excluded from the isolated smoke source copy.
_BUILD_SRC_EXCLUDE = {".git", ".orthos", "build", "dist", "__pycache__", "debian"}


def prepare_smoke_build_source(repo_path: Path, orthos_path: Path) -> Path:
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


def _run_smoke_build_step(
    build_src: Path,
    original_orthos: Path,
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


def _run_build_step(repo_path: str) -> tuple[int, dict[str, Any] | None]:
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
def _cmd_smoke(args: argparse.Namespace) -> int:
    """Run the full pipeline, install packages, and resolve dependencies."""
    repo_path = args.repo_path
    repo = Path(repo_path)
    orthos = orthos_dir(repo)

    if args.host:
        # Pre-isolation host mode: explicit opt-in.
        info("convergence: mode = host (pre-isolation; --host flag set)")
        runner: RunnerProtocol = HostRunner()
        rc = _run_convergence_loop(repo_path, runner)
    else:
        # Isolated chroot mode: default authoritative path.
        # The chroot is shared across projects at .orthos/chroots/<suite>-<repo_set>-<arch>/
        # so that .orthos/<repo>/ contains only user-owned files and can be
        # removed with plain rm -rf without requiring sudo.
        # The convergence build dir is placed under .orthos/chroot-work/ (also
        # outside the project workspace) because Meson runs as root inside the
        # chroot and would leave root-owned files in any bind-mounted directory.
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
            rc = _run_convergence_loop(repo_path, runner)
        finally:
            # Primary cleanup guarantee: always teardown mounts.
            env.teardown_mounts()

        if rc != 0:
            return rc

        info(
            "smoke: convergence ran in chroot - "
            "stage/build pipeline runs on host in this round"
        )

    if rc != 0:
        return rc

    # scan → stage → inventory → classify → generate (no apply)
    chroot_dir_name = f"{args.chroot_suite}-{args.target_repo_set}" if hasattr(args, "target_repo_set") else args.chroot_suite
    _chroot_path = str(shared_chroot_dir(chroot_dir_name)) if not args.host else None
    rc = _run_smoke_prebuild_pipeline(repo_path, chroot_path=_chroot_path)
    if rc != 0:
        return rc

    # Create an isolated source copy and inject generated debian/ into it.
    generated_debian = orthos / "debian"
    if not generated_debian.is_dir():
        error(f"smoke: generated debian/ not found: {generated_debian}")
        return 1

    build_src = prepare_smoke_build_source(repo, orthos)
    info(f"smoke: building from isolated source copy: {build_src}")
    copy_generated_debian_to_build_source(generated_debian, build_src)

    # Build from the isolated copy; results go to the original orthos dir.
    # Forward the chroot path so artifact validation uses the target oracle.
    if not args.host:
        import json
        import shutil
        from deb.privileged.client import chroot_exec
        from deb.resolution.oracle import make_oracle
        from deb.resolution.debian import validate_built_debs

        artifacts_dir = orthos / "artifacts"
        ensure_dir(artifacts_dir)
        for p in artifacts_dir.glob("*.deb"):
            p.unlink()

        try:
            env.setup_mounts(
                source_repo=repo,
                build_dir=convergence_build_dir,
                logs_dir=logs_dir,
                build_src=build_src,
            )
            
            info("smoke: running dpkg-buildpackage inside chroot")
            
            ok, output = chroot_exec(
                env.root,
                ["bash", "-c", "cd /orthos/build-src && apt-get update && apt-get build-dep -y ."]
            )
            build_log = logs_dir / "smoke-chroot-build.log"
            build_log.write_text(output, encoding="utf-8")
            
            if not ok:
                error(f"smoke: chroot build-dep failed. see log: {build_log}")
                return 1

            ok, output2 = chroot_exec(
                env.root,
                ["bash", "-c", "cd /orthos/build-src && dpkg-buildpackage -us -uc -b"]
            )
            with open(build_log, "a", encoding="utf-8") as f:
                f.write("\n" + output2)
                
            if not ok:
                error(f"smoke: chroot build failed. see log: {build_log}")
                return 1

            chroot_orthos = env.root / "orthos"
            for p in chroot_orthos.glob("*.deb"):
                dst = artifacts_dir / p.name
                shutil.copy(str(p), str(dst))
                
        finally:
            env.teardown_mounts()
            
        debs = sorted(str(p) for p in artifacts_dir.glob("*.deb"))
        if not debs:
            error("no .deb artifacts found in chroot build")
            return 1

        gen_result_file = orthos / "generate-result.json"
        generated_pkg_names: frozenset[str] = frozenset()
        if gen_result_file.exists():
            try:
                gen_result = json.loads(gen_result_file.read_text(encoding="utf-8"))
                generated_pkg_names = frozenset(gen_result.get("binary_packages", []))
            except Exception:
                pass
                
        oracle = make_oracle(env.root)
        try:
            validate_built_debs(debs, generated_pkg_names, oracle)
        except RuntimeError as exc:
            error(str(exc))
            return 1
            
        info("smoke: build complete (chroot)")
        for p in debs:
            info(f"  artifact: {p}")

    else:
        rc, result = _run_smoke_build_step(build_src, orthos, chroot_path=None)
        if result is None or not result["success"]:
            return rc

        debs = sorted(p for p in result["artifacts"] if p.endswith(".deb"))
        if not debs:
            error("no .deb artifacts found")
            return 1

        info("smoke: build complete (host)")
        for p in debs:
            info(f"  artifact: {p}")

    if not args.install_host:
        info("smoke: host install skipped (build-only mode)")
        info("smoke: use --install-host to install artifacts on this system")
        info("smoke complete ✔")
        return 0

    info("smoke: installing artifacts on host (--install-host)")
    rc = _install_built_debs(debs)
    if rc != 0:
        return rc

    info("smoke complete ✔")
    return 0


def _cmd_reset_chroot(repo_path: str, suite: str = "trixie") -> int:
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


def _cmd_analyze(repo_path: str) -> int:
    """Read build-result.json and build.log and emit an analysis summary."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result, analyze_file = run_analyze(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

    status = "success" if result["success"] else "failure"
    info(f"repo:    {meta['repo_path']}")
    info(f"result:  {status}")

    if not result["success"]:
        info(f"category: {result['category']}")
        info(f"summary:  {result['summary']}")
        if result["log_excerpt"]:
            info("excerpt:")
            for line in result["log_excerpt"]:
                info(f"  {line}")
    else:
        info(f"summary:  {result['summary']}")

    info(f"wrote:   {analyze_file}")
    return 0


def _cmd_suggest(repo_path: str) -> int:
    """Read analyze-result.json and emit a structured suggestion."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result, suggest_file = run_suggest(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

    status = "success" if result["success"] else "failure"
    info(f"repo:    {meta['repo_path']}")
    info(f"result:  {status}")

    if result["category"]:
        info(f"category: {result['category']}")

    if result["suggestion_type"]:
        info(f"type:     {result['suggestion_type']}")

    if result["target_file"]:
        info(f"target:   {result['target_file']}")

    if result["suggested_change"]:
        info(f"change:   {result['suggested_change']}")

    if result["next_step"]:
        info(f"next:     {result['next_step']}")

    if result["suggested_command"]:
        info(f"command:  {result['suggested_command']}")

    info(f"confidence: {result['confidence']}")
    info(f"wrote:   {suggest_file}")
    return 0


def main() -> None:
    """Run the orthos-packager command-line interface."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    handlers = {
        "scan": lambda: _cmd_scan(args.repo_path),
        "stage": lambda: _cmd_stage(args.repo_path),
        "inventory": lambda: _cmd_inventory(args.repo_path),
        "classify": lambda: _cmd_classify(args.repo_path),
        "generate": lambda: _cmd_generate(args.repo_path),
        "apply": lambda: _cmd_apply(args.repo_path, force=getattr(args, "force", False)),
        "build": lambda: _cmd_build(args.repo_path),
        "analyze": lambda: _cmd_analyze(args.repo_path),
        "suggest": lambda: _cmd_suggest(args.repo_path),
        "smoke": lambda: _cmd_smoke(args),
        "reset-chroot": lambda: _cmd_reset_chroot(
            args.repo_path,
            suite=getattr(args, "chroot_suite", "trixie"),
        ),
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    sys.exit(handler())


if __name__ == "__main__":
    main()
