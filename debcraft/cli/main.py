"""orthos-packager – entry point."""

import argparse
import sys
import glob
import subprocess
from pathlib import Path

from debcraft.analyze import analyze as run_analyze
from debcraft.backends.build_backend_debian import build as run_build
from debcraft.backends.build_backend_meson import stage as meson_stage
from debcraft.build_deps import (
    install_missing_build_dependencies,
    install_missing_pkgconfig_dependencies,
    resolve_build_dependencies,
    scan_meson_dependencies,
    validate_pkg_config_closure,
)
from debcraft.classifier.artifact_classifier import classify as run_classify
from debcraft.core.repo_probe import probe
from debcraft.generator.debian_generator import generate as run_generate
from debcraft.inventory.install_inventory import build_inventory
from debcraft.suggest import suggest as run_suggest
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import error, info

_ORTHOS_DIR = ".orthos"
_META_FILE = "package-meta.json"


def _orthos_dir(repo_path: Path) -> Path:
    """Return the scratch directory for a target repository."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


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

    build_p = sub.add_parser(
        "build", help="Build Debian packages from the generated skeleton.")
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

    return parser


def _cmd_scan(repo_path: str) -> int:
    """Run the scan command and write package metadata JSON."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    out_dir = _orthos_dir(Path(meta["repo_path"]))
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
    except FileNotFoundError as exc:
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
    except FileNotFoundError as exc:
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
    except FileNotFoundError as exc:
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


def _cmd_build(repo_path: str) -> int:
    """Copy generated debian/ into the repo and run dpkg-buildpackage."""
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
    info(f"debian (generated): {result['generated_debian_dir']}")
    info(f"debian (target):    {result['target_debian_dir']}")
    info(f"log:     {result['log_file']}")

    if result["success"]:
        info("result:  success")
        info(f"artifacts: {len(result['artifacts'])}")
        for p in result["artifacts"]:
            info(f"  {p}")
    else:
        step = result.get("failure_step") or "unknown"
        error(f"build failed at: {step}")
        error(f"see log: {result['log_file']}")

    return rc


def _log_and_install_meson_deps(names: list[str]) -> int:
    """Resolve + log + install Meson-declared build dependencies."""
    report = resolve_build_dependencies(names)
    for result in report.results:
        if result.package is None:
            error(
                f"build-dep unresolved: {result.meson_name} — {result.warning}")
            continue
        flag = " [installed]" if result.is_installed else ""
        bodhi_tag = " (bodhi)" if result.is_bodhi else ""
        info(f"build-dep resolved: {result.meson_name} -> "
             f"{result.package}{bodhi_tag} [source: {result.source}]{flag}")
        if result.warning:
            info(f"  warning: {result.warning}")

    unresolved = report.unresolved_names()
    if unresolved:
        error("cannot continue: unresolved build dependencies: "
              f"{', '.join(unresolved)}")
        return 1

    missing = report.missing_packages()
    if not missing:
        info("build-deps: all resolved packages already installed")
        return 0

    info(f"build-deps: installing {len(missing)} missing packages: "
         f"{', '.join(missing)}")
    rc = install_missing_build_dependencies(report)
    if rc != 0:
        error("build-deps: apt install failed")
    return rc


def _run_pkgconfig_closure(names: list[str]) -> int:
    """Validate pkg-config closure and install anything still missing."""
    info("pkg-config: validating closure for discovered dependency names")
    for name in names:
        info(f"pkg-config: checking {name}")
    closure = validate_pkg_config_closure(names)

    for pkg_name, (required_by, pkg) in sorted(closure.missing.items()):
        resolved = pkg if pkg else "(unresolved)"
        info(f"pkg-config missing: {pkg_name} (required by {required_by}) "
             f"-> {resolved}")
    for w in closure.warnings:
        info(f"  warning: {w}")

    if closure.unresolved:
        for name in closure.unresolved:
            error(f"pkg-config: cannot satisfy '{name}' after "
                  f"{closure.retries} retries")
        return 1

    if not closure.missing:
        info("pkg-config closure satisfied")
        return 0

    to_install = sorted({pkg for (_, pkg) in closure.missing.values() if pkg})
    info(f"pkg-config deps: installing {len(to_install)} missing "
         f"package(s): {', '.join(to_install)}")
    rc = install_missing_pkgconfig_dependencies(closure)
    if rc != 0:
        error("pkg-config deps: apt install failed")
        return rc

    closure2 = validate_pkg_config_closure(names)
    if not closure2.all_satisfied():
        for name in closure2.unresolved:
            error(f"pkg-config: still failing after install: {name}")
        return 1

    info("pkg-config closure satisfied")
    return 0


def _resolve_and_install_build_deps(repo_path: str) -> int:
    """Scan meson.build, resolve+install build deps, then close pkg-config."""
    repo = Path(repo_path)
    names = scan_meson_dependencies(repo)
    if not names:
        info("build-deps: no meson dependency() declarations found")
        return 0

    info(f"build-deps: discovered {len(names)} meson dependency names")
    rc = _log_and_install_meson_deps(names)
    if rc != 0:
        return rc
    return _run_pkgconfig_closure(names)


def _cmd_smoke(repo_path: str) -> int:
    """Build, install, and resolve dependencies for a repository."""
    # Resolve + install build dependencies before the pipeline runs.
    rc = _resolve_and_install_build_deps(repo_path)
    if rc != 0:
        return rc

    steps = [
        _cmd_scan,
        _cmd_stage,
        _cmd_inventory,
        _cmd_classify,
        _cmd_generate,
        _cmd_build,
    ]

    # run full pipeline
    for step in steps:
        rc = step(repo_path)
        if rc != 0:
            return rc

    # install artifacts
    debs = sorted(glob.glob(f"{repo_path}-*.deb"))
    if not debs:
        error("no .deb artifacts found")
        return 1

    info(f"installing: {', '.join(debs)}")

    rc = subprocess.call(["sudo", "dpkg", "-i", *debs])
    if rc != 0:
        info("dpkg reported issues, attempting to fix dependencies...")

    # resolve dependencies
    rc = subprocess.call(["sudo", "apt", "-f", "install", "-y"])
    if rc != 0:
        error("apt failed to resolve dependencies")
        return rc

    info("smoke test complete ✔")
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

    if args.command == "scan":
        sys.exit(_cmd_scan(args.repo_path))

    if args.command == "stage":
        sys.exit(_cmd_stage(args.repo_path))

    if args.command == "inventory":
        sys.exit(_cmd_inventory(args.repo_path))

    if args.command == "classify":
        sys.exit(_cmd_classify(args.repo_path))

    if args.command == "generate":
        sys.exit(_cmd_generate(args.repo_path))

    if args.command == "build":
        sys.exit(_cmd_build(args.repo_path))

    if args.command == "analyze":
        sys.exit(_cmd_analyze(args.repo_path))

    if args.command == "suggest":
        sys.exit(_cmd_suggest(args.repo_path))

    if args.command == "smoke":
        sys.exit(_cmd_smoke(args.repo_path))


if __name__ == "__main__":
    main()
