"""orthos-packager – entry point."""

import sys
from pathlib import Path
from typing import Any

from deb.core.repo_probe import probe as _core_probe
from deb.discovery.upstream_metadata import probe_upstream_metadata
from deb.cli.parser import build_parser
from deb.cli.options import parse_meson_options
from deb.cli.commands.scan import cmd_scan
from deb.cli.commands.stage import cmd_stage
from deb.cli.commands.inventory import cmd_inventory
from deb.cli.commands.classify import cmd_classify
from deb.cli.commands.generate import cmd_generate
from deb.cli.commands.apply import cmd_apply
from deb.cli.commands.build import cmd_build
from deb.cli.commands.analyze import cmd_analyze
from deb.cli.commands.suggest import cmd_suggest
from deb.cli.commands.config import cmd_config
from deb.cli.commands.package import cmd_package, cmd_reset_chroot


def probe(repo_path: str) -> dict[str, Any]:
    """Probe the repository and merge upstream metadata."""
    meta = _core_probe(repo_path)
    try:
        up_meta = probe_upstream_metadata(Path(meta["repo_path"]))
        for k, v in up_meta.items():
            if v:
                meta[k] = v
    except OSError:
        pass
    return meta


def main() -> None:
    """Run the orthos-packager command-line interface."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    # Bind probe into simple-command lambdas so handlers stay dependency-free.
    def _scan(rp: str) -> int:
        return cmd_scan(rp, probe)

    def _stage(rp: str, meson_options=None) -> int:
        return cmd_stage(rp, probe, meson_options=meson_options)

    def _inventory(rp: str) -> int:
        return cmd_inventory(rp, probe)

    def _classify(rp: str) -> int:
        return cmd_classify(rp, probe)

    def _generate(rp: str, chroot_path=None, meson_options=None) -> int:
        return cmd_generate(rp, probe, chroot_path=chroot_path, meson_options=meson_options)

    handlers = {
        "scan": lambda: _scan(args.repo_path),
        "stage": lambda: _stage(
            args.repo_path,
            meson_options=parse_meson_options(getattr(args, "meson_options", [])) or None,
        ),
        "inventory": lambda: _inventory(args.repo_path),
        "classify": lambda: _classify(args.repo_path),
        "generate": lambda: _generate(
            args.repo_path,
            meson_options=parse_meson_options(getattr(args, "meson_options", [])) or None,
        ),
        "apply": lambda: cmd_apply(args.repo_path, probe, force=getattr(args, "force", False)),
        "build": lambda: cmd_build(
            args.repo_path,
            probe,
            meson_options=parse_meson_options(getattr(args, "meson_options", [])) or None,
        ),
        "analyze": lambda: cmd_analyze(args.repo_path, probe),
        "suggest": lambda: cmd_suggest(args.repo_path, probe),
        "package": lambda: cmd_package(args, probe, _scan, _stage, _inventory, _classify, _generate),
        "reset-chroot": lambda: cmd_reset_chroot(
            args.repo_path,
            suite=getattr(args, "chroot_suite", "trixie"),
        ),
        "config": lambda: cmd_config(getattr(args, "config_command", "")),
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help(sys.stderr)
        sys.exit(1)

    sys.exit(handler())


if __name__ == "__main__":
    main()
