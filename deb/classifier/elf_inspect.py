"""ELF inspection helpers for file classification support.

These utilities are used only to SUPPORT classification decisions and
never override safe conservative defaults.  Both helpers are best-effort:
they return False/None on any subprocess or I/O error so that callers can
fall back to the safe default.
"""

import subprocess
from pathlib import Path


def is_elf(path: Path) -> bool:
    """Return True when *path* begins with the ELF magic bytes (\\x7fELF).

    This is a fast, dependency-free check that reads only the first four
    bytes of the file.  Returns False on any I/O error or if the path is
    a symlink pointing to a non-existent target.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def has_soname(path: Path) -> bool:
    """Return True when *path* carries a DT_SONAME dynamic entry.

    Uses 'objdump -p' to parse the dynamic section.  A shared library
    intended for linking (i.e. one that 'ldconfig' would register) will
    always have a SONAME.  Plugin/runtime-loaded objects typically do not.

    Returns False on any subprocess error, missing binary, or non-ELF input.
    """
    try:
        result = subprocess.run(
            ["objdump", "-p", str(path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return False
        return "SONAME" in result.stdout
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
