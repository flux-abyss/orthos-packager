"""Apt oracle abstraction for target-aware dependency validation.

The 'AptOracle' interface decouples dependency validation from the
process-local 'apt-cache' command, making it possible to validate
dependencies against the actual *target* Debian environment rather than
the host running the build.

Without target awareness, 'apt-cache policy' reflects whichever packages
are installed on the build host.  When a host has non-Debian libraries
(e.g. a custom EFL build) their host-side shlibs metadata teaches apt-cache
to report them as valid — even though they do not exist in the target Debian
archive.  The oracle abstraction makes this distinction explicit and
enforceable.

Classes
-------
'AptOracle' (ABC)
    Protocol that all oracles implement: 'package_exists(name) -> bool'.

'HostAptOracle'
    Queries the build host's 'apt-cache policy'.  This is the fallback
    when no chroot is available; its results may be contaminated by
    non-Debian packages installed on the host.

'ChrootAptOracle'
    Runs 'chroot <path> apt-cache policy <pkg>' so that the query uses
    the package database of the target Debian environment, not the host's.
    This is the authoritative oracle when a chroot is in use.

Factory
-------
'make_oracle(chroot_path)'
    Return the appropriate oracle for *chroot_path*: a
    :class:`ChrootAptOracle` when the path is a non-empty directory,
    :class:`HostAptOracle` otherwise (with a warning logged).
"""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from deb.utils.log import info


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class AptOracle(ABC):
    """Validate Debian package existence against a specific apt environment.

    Subclasses implement :meth:`package_exists` using the tool and environment
    appropriate for their scope (host or chroot).
    """

    @abstractmethod
    def package_exists(self, name: str) -> bool:
        """Return True when *name* is an installable Debian package.

        Implementations must:
        * Strip version constraints from *name* before querying.
        * Pass through debhelper substitution variables ('${...}')
          unconditionally (they are always valid in 'debian/control').
        * Return False on any subprocess error or timeout.

        Args:
            name: A package name as it would appear in a Depends field,
                  optionally with a version constraint, e.g.
                  'libc6 (>= 2.38)' or just 'libc6'.

        Returns:
            True if the package has a non-'(none)' Candidate in the
            target apt database.
        """

    # ------------------------------------------------------------------
    # Shared helpers available to all subclasses
    # ------------------------------------------------------------------

    @staticmethod
    def _bare_name(name: str) -> str:
        """Strip version constraints and return the bare package name."""
        return name.split("(")[0].strip()

    @staticmethod
    def _parse_candidate(stdout: str) -> bool:
        """Return True when *stdout* contains a non-'(none)' Candidate line."""
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Candidate:"):
                candidate = stripped.split(":", 1)[1].strip()
                return candidate not in ("", "(none)")
        return False

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"


# ---------------------------------------------------------------------------
# Host oracle
# ---------------------------------------------------------------------------

class HostAptOracle(AptOracle):
    """Query the build host's apt database via 'apt-cache policy'.

    This is the fallback oracle.  Its results reflect the host's package
    database, which may include non-Debian packages (contamination).

    .. warning::
        Use only when no chroot oracle is available.  Validation with this
        oracle may miss invalid deps that exist on the host but not in the
        target Debian archive.
    """

    def package_exists(self, name: str) -> bool:
        bare = self._bare_name(name)
        if not bare or bare.startswith("${"):
            return True
        try:
            result = subprocess.run(
                ["apt-cache", "policy", bare],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False
        return self._parse_candidate(result.stdout)


# ---------------------------------------------------------------------------
# Chroot oracle
# ---------------------------------------------------------------------------

class ChrootAptOracle(AptOracle):
    """Query a Debian chroot's apt database via 'chroot <path> apt-cache policy'.

    This is the authoritative oracle when Orthos is building inside a clean
    Debian chroot.  It uses 'sudo chroot' so that apt-cache can access the
    chroot's dpkg database, which contains only genuine Debian packages.

    Args:
        chroot_path: Absolute path to the root of the Debian chroot.
    """

    def __init__(self, chroot_path: str | Path) -> None:
        self._chroot = Path(chroot_path)

    def package_exists(self, name: str) -> bool:
        bare = self._bare_name(name)
        if not bare or bare.startswith("${"):
            return True
        try:
            result = subprocess.run(
                ["sudo", "chroot", str(self._chroot), "apt-cache", "policy", bare],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0 or not result.stdout.strip():
            return False
        return self._parse_candidate(result.stdout)

    def __repr__(self) -> str:
        return f"ChrootAptOracle(chroot={self._chroot})"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_oracle(chroot_path: str | Path | None) -> AptOracle:
    """Return the best available oracle for dependency validation.

    If *chroot_path* is a non-empty directory, returns a
    :class:`ChrootAptOracle` that queries that chroot's package database —
    this is the authoritative, target-scoped oracle.

    Otherwise (path is 'None', empty, or does not exist), returns a
    :class:`HostAptOracle` with a warning logged.  Host-scoped validation
    may be contaminated if the build host has non-Debian packages installed.

    Args:
        chroot_path: Path to the chroot root directory, or 'None'.

    Returns:
        An :class:`AptOracle` instance.
    """
    if chroot_path:
        p = Path(chroot_path)
        if p.is_dir():
            info(f"apt-oracle: using chroot scope — {p}")
            return ChrootAptOracle(p)

    info(
        "apt-oracle: WARNING — no chroot path available; "
        "falling back to host-scoped apt-cache. "
        "Validation may be contaminated by non-Debian host packages."
    )
    return HostAptOracle()
