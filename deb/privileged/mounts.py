"""Mount helpers for orthos-priv."""

from __future__ import annotations

import subprocess
from pathlib import Path

from deb.privileged.protocol import _log


def _is_mounted(path: Path) -> bool:
    """Return True if *path* appears as a mount point in /proc/mounts."""
    path_str = str(path)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path_str:
                    return True
    except OSError:
        pass
    return False


def _mount_bind(src: str | Path, dst: Path, read_only: bool = False) -> None:
    """Bind-mount *src* to *dst*. Raises RuntimeError on failure.

    For read-only bind mounts, uses a two-step approach:
      1. mount --bind <src> <dst>
      2. mount -o bind,remount,ro <dst>
    Both steps must succeed; failure at either raises RuntimeError.
    """
    dst.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mount", "--bind", str(src), str(dst)],
        check=False,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise RuntimeError(f"failed to bind-mount {src} -> {dst}: {err}")
    _log(f"orthos-priv: mount --bind {src} -> {dst}")
    if read_only:
        ro_result = subprocess.run(
            ["mount", "-o", "bind,remount,ro", str(dst)],
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ro_result.returncode != 0:
            err = (ro_result.stderr or "").strip()
            # The bind mount succeeded but the remount failed.
            # Attempt to undo the bind mount before raising so the caller
            # does not have to guess what was partially done.
            subprocess.run(["umount", str(dst)], check=False)
            raise RuntimeError(
                f"failed to remount {dst} read-only (exit {ro_result.returncode}): {err}"
            )
        _log(f"orthos-priv: remount ro {dst}")


def _mount_special(fstype: str, dst: Path) -> None:
    """Mount a special filesystem (*fstype*) at *dst*. Raises RuntimeError on failure."""
    dst.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mount", "-t", fstype, fstype, str(dst)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to mount {fstype} at {dst}")
    _log(f"orthos-priv: mount {fstype} -> {dst}")
