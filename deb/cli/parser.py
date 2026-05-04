"""Parser construction for orthos-packager CLI."""

import argparse


def _add_package_args(p: argparse.ArgumentParser) -> None:
    """Attach the shared positional/optional arguments for the package command."""
    p.add_argument(
        "repo_path",
        metavar="PATH",
        help="Local path to the repository.",
    )
    mod_group = p.add_mutually_exclusive_group()
    mod_group.add_argument(
        "--host",
        action="store_true",
        default=False,
        help=(
            "Run convergence directly on the host (pre-isolation mode). "
            "Default (without this flag) is isolated chroot mode."
        ),
    )
    p.add_argument(
        "--refresh-chroot",
        action="store_true",
        default=False,
        help="Delete and recreate the chroot before running.",
    )
    p.add_argument(
        "--chroot-suite",
        metavar="SUITE",
        default="trixie",
        help="Debian suite for chroot creation (default: trixie).",
    )
    p.add_argument(
        "--target-repo-set",
        choices=["debian", "bodhi"],
        default="debian",
        help="Target package universe for chroot creation (default: debian).",
    )
    p.add_argument(
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
    p.add_argument(
        "--meson-option",
        metavar="KEY=VALUE",
        action="append",
        dest="meson_options",
        default=[],
        help="Pass a Meson option as KEY=VALUE (may be repeated).",
    )


def build_parser() -> argparse.ArgumentParser:
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
    stage.add_argument(
        "--meson-option",
        metavar="KEY=VALUE",
        action="append",
        dest="meson_options",
        default=[],
        help="Pass a Meson option as KEY=VALUE (may be repeated).",
    )

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
    generate.add_argument(
        "--meson-option",
        metavar="KEY=VALUE",
        action="append",
        dest="meson_options",
        default=[],
        help="Pass a Meson option as KEY=VALUE (may be repeated).",
    )

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
    build_p.add_argument(
        "--meson-option",
        metavar="KEY=VALUE",
        action="append",
        dest="meson_options",
        default=[],
        help="Pass a Meson option as KEY=VALUE (may be repeated).",
    )

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

    # Primary full-pipeline command.
    package_p = sub.add_parser(
        "package",
        help=(
            "Run convergence + full packaging pipeline. "
            "Build-only by default; use --install-host to install artifacts on this system."
        ),
    )
    _add_package_args(package_p)

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

    config_p = sub.add_parser(
        "config",
        help="Manage orthos configuration.",
    )
    config_p.add_argument(
        "config_command",
        choices=["init", "show"],
        help="Config action to perform.",
    )

    return parser
