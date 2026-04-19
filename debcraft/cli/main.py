"""orthos-packager – entry point."""

import argparse
import sys
import subprocess
from pathlib import Path
from typing import Any

from debcraft.analyze import analyze as run_analyze
from debcraft.apply_debian import apply as run_apply
from debcraft.backends.build_backend_debian import build as run_build
from debcraft.backends.build_backend_meson import stage as meson_stage
from debcraft.classifier.artifact_classifier import classify as run_classify
from debcraft.core.repo_probe import probe
from debcraft.discovery.chroot_env import ChrootEnv, ChrootEnvError
from debcraft.discovery.convergence import ConvergenceResult, run_convergence_loop
from debcraft.discovery.runner import ChrootRunner, HostRunner, RunnerProtocol
from debcraft.generator.debian_generator import generate as run_generate
from debcraft.inventory.install_inventory import build_inventory
from debcraft.privileged import client as priv_client
from debcraft.privileged.launcher import PrivilegedHelperError
from debcraft.suggest import suggest as run_suggest
from debcraft.paths import orthos_dir
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import error, info

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
        help="Run full pipeline, install packages, and resolve dependencies.",
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

    reset_chroot_p = sub.add_parser(
        "reset-chroot",
        help="Safely tear down mounts and remove the chroot for a repo.",
    )
    reset_chroot_p.add_argument(
        "repo_path",
        metavar="PATH",
        help="Local path to the repository whose chroot should be reset.",
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


def _cmd_generate(repo_path: str) -> int:
    """Generate a debian/ skeleton from the package plan."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

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
    return rc


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
      0 — converged successfully or stalled (nonfatal; stage step handles it)
      1 — apt install failed inside the loop (fatal; smoke must stop)
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
            info(f"convergence: WARNING — {w}")

    # Fatal: apt install failed inside the convergence loop.
    if result.install_failed:
        error("convergence: apt install failed — aborting smoke")
        return 1

    if result.success:
        info("convergence: meson setup converged — "
             "setup-time dependencies satisfied")
        return 0

    if result.stalled:
        if result.stall_reason == "unresolved":
            info(f"convergence: stalled — "
                 f"{len(result.unresolved_misses)} miss(es) unresolvable:")
            for miss in result.unresolved_misses:
                info(f"  {miss.miss_type}: {miss.name}")
                info(f"    from: {miss.raw_line}")
        else:
            info("convergence: stalled — no new packages to install; "
                 "proceeding to stage")
    else:
        info("convergence: max passes exhausted without setup success; "
             "proceeding to stage")

    # Nonfatal stall — let the stage step fail explicitly so the
    # human maintainer sees a concrete error.
    return 0


def _partition_debs(debs: list[str]) -> tuple[list[str], list[str]]:
    """Return (main_debs, dbgsym_debs) partitioned from *debs*."""
    main_debs = [d for d in debs if "-dbgsym_" not in d]
    dbgsym_debs = [d for d in debs if "-dbgsym_" in d]
    return main_debs, dbgsym_debs


def _run_prebuild_pipeline(repo_path: str) -> int:
    """Run scan→generate pipeline and auto-apply debian/ if the repo lacks one."""
    for step in (
            _cmd_scan,
            _cmd_stage,
            _cmd_inventory,
            _cmd_classify,
            _cmd_generate,
    ):
        rc = step(repo_path)
        if rc != 0:
            return rc

    repo = Path(repo_path)
    if not (repo / "debian").exists():
        info("smoke: repo/debian absent – auto-applying generated debian/")
        return _cmd_apply(repo_path, force=False)

    info("smoke: repo/debian present – skipping auto-apply")
    return 0


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


# pylint: disable=too-many-return-statements
def _cmd_smoke(args: argparse.Namespace) -> int:
    """Run the full pipeline, install packages, and resolve dependencies."""
    repo_path = args.repo_path
    repo = Path(repo_path)

    if args.host:
        # Pre-isolation host mode: explicit opt-in.
        info("convergence: mode = host (pre-isolation; --host flag set)")
        runner: RunnerProtocol = HostRunner()
        rc = _run_convergence_loop(repo_path, runner)
    else:
        # Isolated chroot mode: default authoritative path.
        orthos = orthos_dir(repo)
        chroot_root = orthos / "chroot"
        logs_dir = orthos / "logs"
        build_dir = orthos / "build"
        ensure_dir(logs_dir)
        ensure_dir(build_dir)

        chroot_log = logs_dir / "chroot-setup.log"
        env = ChrootEnv(chroot_root)
        try:
            env.ensure_ready(
                suite=args.chroot_suite,
                refresh=args.refresh_chroot,
                log_file=chroot_log,
            )
        except ChrootEnvError as exc:
            error(f"convergence: chroot setup failed: {exc}")
            return 1

        try:
            env.setup_mounts(
                source_repo=repo,
                build_dir=build_dir,
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

        # Fix 4: make isolation scope explicit after convergence completes.
        info(
            "smoke: convergence ran in chroot — "
            "stage/build pipeline runs on host in this round"
        )

    if rc != 0:
        return rc

    rc = _run_prebuild_pipeline(repo_path)
    if rc != 0:
        return rc

    rc, result = _run_build_step(repo_path)
    if result is None or not result["success"]:
        return rc

    debs = sorted(p for p in result["artifacts"] if p.endswith(".deb"))
    if not debs:
        error("no .deb artifacts found")
        return 1

    rc = _install_built_debs(debs)
    if rc != 0:
        return rc

    info("smoke test complete ✔")
    return 0


def _cmd_reset_chroot(repo_path: str) -> int:
    """Tear down mounts and remove the chroot workspace for a repo.

    Reads /proc/mounts (via the privileged helper) to find any active mounts
    under the chroot root, unmounts them, then removes the chroot tree.
    The user does not need to run sudo umount or sudo rm -rf manually.
    """
    repo = Path(repo_path)
    chroot_root = orthos_dir(repo) / "chroot"
    info(f"reset-chroot: target: {chroot_root}")
    try:
        priv_client.reset_chroot(chroot_root)
    except PrivilegedHelperError as exc:
        error(f"reset-chroot: failed: {exc}")
        return 1
    info(f"reset-chroot: done: {chroot_root}")
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
        "reset-chroot": lambda: _cmd_reset_chroot(args.repo_path),
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    sys.exit(handler())


if __name__ == "__main__":
    main()
