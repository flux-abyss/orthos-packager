"""orthos-packager – entry point."""

import argparse
import sys
from pathlib import Path

from debcraft.core.repo_probe import probe
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import error, info

_ORTHOS_DIR = ".orthos"
_META_FILE = "package-meta.json"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orthos-packager",
        description="Deterministic Debian packager for Meson projects.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    scan = sub.add_parser("scan", help="Probe a repository and emit package metadata.")
    scan.add_argument("repo_path", metavar="PATH", help="Local path to the repository.")

    return parser


def _cmd_scan(repo_path: str) -> int:
    try:
        meta = probe(repo_path)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1
    except NotADirectoryError as exc:
        error(str(exc))
        return 1
    except ValueError as exc:
        error(str(exc))
        return 1

    # Write metadata relative to the probed repo, not cwd.
    out_dir = Path(meta["repo_path"]) / _ORTHOS_DIR
    ensure_dir(out_dir)
    out_file = out_dir / _META_FILE
    write_json(out_file, meta)

    # Terminal summary.
    name = meta["project_name"] or "(unknown)"
    version = meta["version"] or "(unknown)"
    debian = "yes" if meta["debian_dir"] else "no"

    info(f"repo:    {meta['repo_path']}")
    info(f"project: {name}  version: {version}")
    info(f"debian/: {debian}")
    info(f"wrote:   {out_file}")

    return 0


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    if args.command == "scan":
        sys.exit(_cmd_scan(args.repo_path))


if __name__ == "__main__":
    main()
