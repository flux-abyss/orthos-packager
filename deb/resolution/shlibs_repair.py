"""Shlibs dependency repair diagnostics.

When 'dh_shlibdeps' injects an invalid Debian package name (e.g.
'libefl') into a built '.deb''s Depends field, the build host's shlibs
metadata is contaminated — it names a package that does not exist in the
target Debian archive.

This module is purely diagnostic: it does **not** rewrite built '.deb'
files.  Instead, for each invalid dependency bare name it tries to identify
the real Debian runtime package that provides the underlying shared object,
and reports that information so the maintainer knows exactly how to fix the
build environment.

Resolution strategy
-------------------
For a bad dep name such as 'libefl':

A. Derive candidate SONAME patterns:
   The dep name is used as a stem to form plausible '.so' glob patterns::

       libefl.so, libefl.so.*, /libefl.so, /libefl.so.*

B. Resolve provider package:

   1. 'dpkg -S <soname-path>' — queries the local dpkg database for the
      installed package that owns the library file.  This is the fastest path
      and works correctly inside a properly configured Debian chroot.

   2. 'apt-file search <soname-pattern>' — searches the apt-file index for
      any package providing a file matching the pattern.  Used when dpkg -S
      finds nothing (file not installed on the build host).

C. Filter results:
   - Runtime packages are preferred (those NOT ending in '-dev', '-dbg',
     '-doc', '-data').
   - If only '-dev' providers are found, the result is classified as
     'dev_only' and the diagnostic flags this explicitly — a '-dev'
     package is never a valid runtime Depends.
   - If nothing is found, the result is 'unresolved'.

Public API
----------
'repair_shlibs_dep(bad_dep_name)'
    Given a bare invalid dep name (e.g. 'libefl'), return a
    :class:`RepairResult` describing the resolution outcome.

'repair_shlibs_deps(bad_dep_names)'
    Batch version: returns a list of :class:`RepairResult` objects, one per
    name.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

# Suffixes that identify non-runtime binary packages.  We prefer to exclude
# these from runtime Depends recommendations.
_NON_RUNTIME_SUFFIXES = ("-dev", "-dbg", "-doc", "-data", "-common")


@dataclass
class RepairResult:
    """Resolution outcome for one invalid shlibs dependency name.

    Attributes
    ----------
    bad_dep:
        The bare invalid dependency name as it appears in the .deb Depends
        field (e.g. 'libefl').
    soname_patterns:
        The SONAME glob patterns derived from *bad_dep* that were searched
        (e.g. '["libefl.so", "libefl.so.*"]').
    dpkg_provider:
        Package name found via 'dpkg -S', or 'None'.
    aptfile_candidates:
        Packages found via 'apt-file search', in discovery order.
    runtime_providers:
        Subset of all found candidates that are not '-dev'/'-dbg'/etc.
        This is the preferred list for a runtime Depends.
    dev_only:
        True when providers were found but *all* of them are non-runtime
        ('-dev', '-dbg', etc.).  A '-dev' package is never a safe
        runtime Depends entry.
    resolved:
        True when at least one runtime provider was found.
    recommended:
        The single best runtime package to use in Depends, or 'None'.
    """

    bad_dep: str
    soname_patterns: list[str] = field(default_factory=list)
    dpkg_provider: str | None = None
    aptfile_candidates: list[str] = field(default_factory=list)
    runtime_providers: list[str] = field(default_factory=list)
    dev_only: bool = False
    resolved: bool = False
    recommended: str | None = None

    def format_diagnostic(self) -> str:
        """Return a human-readable multi-line diagnostic string."""
        lines: list[str] = [
            f"  shlibs-repair: bad dep '{self.bad_dep}'",
            f"    soname patterns: {', '.join(self.soname_patterns)}",
        ]

        if self.dpkg_provider:
            lines.append(f"    dpkg -S provider:  {self.dpkg_provider}")
        else:
            lines.append("    dpkg -S provider:  (not found on build host)")

        if self.aptfile_candidates:
            lines.append(
                f"    apt-file candidates: {', '.join(self.aptfile_candidates)}"
            )
        else:
            lines.append("    apt-file candidates: (none found)")

        if self.resolved:
            lines.append(
                f"    recommended Depends: {self.recommended}"
            )
            lines.append(
                "    action: add a shlibs override or build in a clean chroot "
                "to use the correct package name"
            )
        elif self.dev_only:
            lines.append(
                "    WARNING: only -dev providers found — "
                "a -dev package is NOT a valid runtime Depends entry"
            )
            lines.append(
                "    action: install the runtime library package on the build "
                "host and rebuild in a clean chroot"
            )
        else:
            lines.append(
                "    action: build in a clean Debian chroot where target "
                "library packages are correctly registered in dpkg/shlibs"
            )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _soname_patterns(dep_name: str) -> list[str]:
    """Return SONAME search patterns derived from *dep_name*.

    Given 'libefl', returns::

        ["libefl.so", "libefl.so.*"]

    The patterns are used both for 'dpkg -S' path searches and for
    'apt-file search' queries.  We use the simplest plausible set: the
    exact '.so' and the versioned '.so.*' glob.
    """
    return [f"{dep_name}.so", f"{dep_name}.so.*"]


def _is_runtime_package(pkg: str) -> bool:
    """Return True when *pkg* looks like a runtime (non-dev/dbg/doc) package."""
    return not any(pkg.endswith(s) for s in _NON_RUNTIME_SUFFIXES)


def _dpkg_search(soname: str) -> str | None:
    """Try 'dpkg -S /<soname>' and return the owning package name, or None.

    The leading '/' is omitted from the search because 'dpkg -S' accepts
    a path fragment; we search for the filename only so it matches any
    directory the library might be installed in.

    Returns the first package name found, or 'None' if nothing matched or
    the command is unavailable.
    """
    try:
        result = subprocess.run(
            ["dpkg", "-S", soname],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    # dpkg -S output: "<package>: <path>\n..."  — take the first match.
    line = result.stdout.strip().splitlines()[0]
    if ":" in line:
        pkg = line.split(":")[0].strip()
        # Strip architecture suffix like "libc6:amd64" → "libc6"
        pkg = pkg.split(":")[0].strip()
        return pkg or None
    return None


def _aptfile_search(pattern: str) -> list[str]:
    """Run 'apt-file search <pattern>' and return matching package names.

    'apt-file' must be installed and its cache updated ('apt-file update')
    for this to work.  Returns an empty list on any error or when apt-file
    is not installed.

    Deduplicates the returned package list while preserving discovery order.
    """
    try:
        result = subprocess.run(
            ["apt-file", "search", pattern],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0 or not result.stdout.strip():
        return []

    seen: set[str] = set()
    packages: list[str] = []
    # apt-file output: "<package>: <path>\n..."
    for line in result.stdout.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        pkg = line.split(":")[0].strip()
        if pkg and pkg not in seen:
            seen.add(pkg)
            packages.append(pkg)
    return packages


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def repair_shlibs_dep(bad_dep_name: str) -> RepairResult:
    """Attempt to resolve a real Debian runtime package for *bad_dep_name*.

    See module docstring for the full resolution algorithm.

    Args:
        bad_dep_name: The bare invalid dependency name, e.g. 'libefl'.
            Must not contain version constraints or whitespace.

    Returns:
        A :class:`RepairResult` populated with whatever the resolution
        process found.  Check 'result.resolved' to determine whether a
        runtime provider was identified.
    """
    patterns = _soname_patterns(bad_dep_name)
    result = RepairResult(bad_dep=bad_dep_name, soname_patterns=patterns)

    all_candidates: list[str] = []

    # Step A+B.1: dpkg -S for each pattern.
    for pattern in patterns:
        pkg = _dpkg_search(pattern)
        if pkg and pkg not in all_candidates:
            all_candidates.append(pkg)
            if result.dpkg_provider is None:
                result.dpkg_provider = pkg

    # Step B.2: apt-file search when dpkg -S found nothing.
    if not all_candidates:
        for pattern in patterns:
            for pkg in _aptfile_search(pattern):
                if pkg not in all_candidates:
                    all_candidates.append(pkg)
        result.aptfile_candidates = list(all_candidates)

    # Step C: filter to runtime providers.
    runtime = [p for p in all_candidates if _is_runtime_package(p)]
    result.runtime_providers = runtime

    if runtime:
        result.resolved = True
        result.recommended = runtime[0]
    elif all_candidates:
        # Providers found but all are -dev/-dbg/etc.
        result.dev_only = True

    return result


def repair_shlibs_deps(bad_dep_names: list[str]) -> list[RepairResult]:
    """Resolve real runtime providers for each name in *bad_dep_names*.

    Convenience wrapper around :func:`repair_shlibs_dep`.

    Args:
        bad_dep_names: Bare invalid dependency names (no version constraints).

    Returns:
        List of :class:`RepairResult`, one per name, in the same order.
    """
    return [repair_shlibs_dep(name) for name in bad_dep_names]
