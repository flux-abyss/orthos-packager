"""orthos-packager – entry point."""

import argparse
import sys
from pathlib import Path

from debcraft.backends.build_backend_debian import build as run_build
from debcraft.backends.build_backend_meson import stage as meson_stage
from debcraft.classifier.artifact_classifier import classify as run_classify
from debcraft.core.repo_probe import probe
from debcraft.generator.debian_generator import generate as run_generate
from debcraft.inventory.install_inventory import build_inventory
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


if __name__ == "__main__":
    main()
