"""Artifact partition and install helpers for the orthos package command."""

import subprocess

from deb.utils.log import error, info


def _partition_debs(debs: list[str]) -> tuple[list[str], list[str]]:
    """Return (main_debs, dbgsym_debs) partitioned from *debs*."""
    main_debs = [d for d in debs if "-dbgsym_" not in d]
    dbgsym_debs = [d for d in debs if "-dbgsym_" in d]
    return main_debs, dbgsym_debs


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
