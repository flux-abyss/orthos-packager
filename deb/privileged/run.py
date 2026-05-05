"""Subprocess execution helper for orthos-priv."""

from __future__ import annotations

import subprocess

from deb.privileged.protocol import _log


def _run(cmd: list[str], step: str, log_fh: object = None) -> None:
    """Run *cmd*, optionally appending to *log_fh*. Raises RuntimeError on failure."""
    _log(f"orthos-priv: {step}")
    if log_fh:
        log_fh.write(f"\n# {step}\n$ {' '.join(cmd)}\n")  # type: ignore[union-attr]
        log_fh.flush()  # type: ignore[union-attr]
    result = subprocess.run(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT if log_fh else None,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"step '{step}' failed (exit {result.returncode})"
        )
