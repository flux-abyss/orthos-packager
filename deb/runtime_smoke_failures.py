"""Parse runtime smoke failure output into missing runtime dependency candidates.

This module is intentionally narrow: it only scans output strings for well-known
error patterns and maps them to Debian package candidates.  It does not modify
debian/control, trigger rebuilds, or perform any network/disk I/O.

Supported patterns:
  - ModuleNotFoundError / ImportError  -> python-module candidate
  - bash / sh command not found        -> command candidate
  - FileNotFoundError on an executable -> command candidate
  - GI namespace not available         -> gi-namespace candidate
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from deb.runtime_dependency_inference import (
    CLI_COMMAND_MAP,
    GI_NAMESPACE_MAP,
    PYTHON_IMPORT_MAP,
)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# ModuleNotFoundError: No module named 'yaml'
# ImportError: No module named requests
_RE_MODULE_NOT_FOUND = re.compile(
    r"(?:ModuleNotFoundError|ImportError)[^\n]*No module named '?([A-Za-z_][A-Za-z0-9_.]*)'?"
)

# bash: meson: command not found
# /bin/bash: line N: ninja: command not found
# /bin/sh: 1: ninja: not found
_RE_BASH_CMD_NOT_FOUND = re.compile(
    r"(?:bash|sh)[^:]*:\s+(?:line\s+\d+:\s+)?([A-Za-z0-9_\-\.]+):\s+(?:command )?not found"
)

# FileNotFoundError: [Errno 2] No such file or directory: 'debootstrap'
_RE_FILENOTFOUND_CMD = re.compile(
    r"FileNotFoundError[^\n]*No such file or directory[^\n]*'([A-Za-z0-9_\-\.]+)'"
)

# ValueError: Namespace Gtk not available
# ImportError: cannot import name Gtk from gi.repository
_RE_GI_NS_NOT_AVAILABLE = re.compile(
    r"(?:ValueError|ImportError)[^\n]*"
    r"(?:Namespace ([A-Za-z][A-Za-z0-9_]*) not available"
    r"|cannot import name ([A-Za-z][A-Za-z0-9_]*) from gi\.repository)"
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class RuntimeMissingDependency:
    """A candidate runtime dependency inferred from smoke failure output."""
    kind: str                       # "python-module", "command", "gi-namespace"
    name: str                       # bare name (module name, command, namespace)
    debian_package: str | None      # mapped Debian package, or None if unknown
    evidence: str                   # the error text fragment that triggered this
    source: str = "runtime-smoke"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def infer_missing_runtime_dependencies(
    failures: list[dict[str, Any]],
) -> list[RuntimeMissingDependency]:
    """Scan smoke failure dicts for recognisable missing-dependency patterns.

    Only failures that carry an ``"output"`` or ``"error"`` string are scanned.
    Candidates are deduplicated by (kind, name) while preserving insertion order.

    Args:
        failures: list of failure dicts from RuntimeSmokeResult.failures.

    Returns:
        List of RuntimeMissingDependency instances, deduplicated and ordered by
        first appearance.  Empty list when nothing recognisable is found.
    """
    candidates: list[RuntimeMissingDependency] = []
    seen: set[tuple[str, str]] = set()  # (kind, name)

    for failure in failures:
        output = failure.get("output") or failure.get("error") or ""
        if not isinstance(output, str) or not output:
            continue
        _scan_output(output, candidates, seen)

    return candidates


# ---------------------------------------------------------------------------
# Internal scanning helpers
# ---------------------------------------------------------------------------

def _add(
    candidates: list[RuntimeMissingDependency],
    seen: set[tuple[str, str]],
    kind: str,
    name: str,
    pkg: str | None,
    evidence: str,
) -> None:
    key = (kind, name)
    if key in seen:
        return
    seen.add(key)
    candidates.append(RuntimeMissingDependency(
        kind=kind,
        name=name,
        debian_package=pkg,
        evidence=evidence,
    ))


def _scan_output(
    output: str,
    candidates: list[RuntimeMissingDependency],
    seen: set[tuple[str, str]],
) -> None:
    """Apply all patterns to *output* and populate *candidates*."""

    # Python module errors
    for m in _RE_MODULE_NOT_FOUND.finditer(output):
        raw = m.group(1)
        # Use the top-level module name for map lookup.
        top = raw.split(".")[0]
        pkg = PYTHON_IMPORT_MAP.get(top)
        _add(candidates, seen, "python-module", top, pkg, m.group(0))

    # Bash "command not found"
    for m in _RE_BASH_CMD_NOT_FOUND.finditer(output):
        cmd = m.group(1)
        pkg = CLI_COMMAND_MAP.get(cmd)
        _add(candidates, seen, "command", cmd, pkg, m.group(0))

    # FileNotFoundError on a command name
    for m in _RE_FILENOTFOUND_CMD.finditer(output):
        cmd = m.group(1)
        # Only treat it as a command miss if it looks like an executable name
        # (no path separators), reducing false positives on ordinary files.
        if "/" not in cmd:
            pkg = CLI_COMMAND_MAP.get(cmd)
            _add(candidates, seen, "command", cmd, pkg, m.group(0))

    # GI namespace errors
    for m in _RE_GI_NS_NOT_AVAILABLE.finditer(output):
        ns = m.group(1) or m.group(2)
        if ns:
            pkgs = GI_NAMESPACE_MAP.get(ns)
            # Report only the first mapped package; caller can consult the
            # map directly for the full list if needed.
            pkg = pkgs[0] if pkgs else None
            _add(candidates, seen, "gi-namespace", ns, pkg, m.group(0))


# ---------------------------------------------------------------------------
# Log formatting
# ---------------------------------------------------------------------------

def format_candidates_for_log(
    candidates: list[RuntimeMissingDependency],
) -> str:
    """Return a human-readable log block for *candidates*.

    Returns an empty string when the list is empty.
    """
    if not candidates:
        return ""
    lines = ["# inferred missing runtime dependencies"]
    for c in candidates:
        pkg_str = c.debian_package if c.debian_package else "(unknown)"
        lines.append(f"  {c.name} -> {pkg_str}  [{c.kind}]  evidence: {c.evidence!r}")
    return "\n".join(lines) + "\n"
