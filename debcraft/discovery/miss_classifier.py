"""Classify concrete missing requirements from meson setup output.

Operates only on meson setup stdout+stderr. Does not classify compile-phase
or link-phase output — those are later extension points.

Classification is fully deterministic: fixed substring and regex patterns.
No scoring, no fuzzy inference.

Miss types in scope:
  pkg-config-miss  — missing pkg-config module
  tool-miss        — missing program or tool-type dependency
  header-miss      — missing header (configure-time probe only)
  library-miss     — missing library (configure-time probe only)

Note: header-miss and library-miss coverage is limited to configure-time
Meson probes (cc.has_header, cc.find_library). Misses that only surface
during compilation are not detectable here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DepMiss:
    """A single concrete missing requirement extracted from build output."""

    miss_type: str           # "pkg-config-miss" | "tool-miss" | "header-miss" | "library-miss"
    name: str                # concrete missing name: e.g. "lua51", "wayland-scanner", "foo.h"
    required_by: str | None  # e.g. "edje" from pkg-config required-by output
    raw_line: str            # original log line, preserved verbatim


# ---------------------------------------------------------------------------
# Patterns — meson setup stdout/stderr
# ---------------------------------------------------------------------------

# pkg-config: "Package 'foo', required by 'bar', not found"
# Must be checked before the simpler no-package pattern (more specific).
_RE_PKGCFG_REQUIRED_BY = re.compile(
    r"Package '([^']+)',\s*required by '([^']+)',\s*not found"
)

# pkg-config: "No package 'foo' found"
_RE_PKGCFG_NO_PACKAGE = re.compile(r"No package '([^']+)' found")

# pkg-config: "Package foo was not found in the pkg-config search path."
_RE_PKGCFG_NOT_IN_PATH = re.compile(
    r"Package ([^\s']+) was not found in the pkg-config search path"
)

# Meson find_program: "Program 'foo' not found or not executable"
_RE_PROGRAM_NOT_FOUND = re.compile(
    r"Program '([^']+)' not found or not executable"
)

# Meson find_program: "Program foo found: NO"
# Captured separately so we can deduplicate with _RE_PROGRAM_NOT_FOUND.
_RE_PROGRAM_FOUND_NO = re.compile(
    r"^Program ([^\s:]+) found:\s*NO\b",
    re.IGNORECASE,
)

# Meson dependency(): "Run-time dependency foo found: NO"
# Routed to tool-miss when name is in TOOL_DEP_MAP, else pkg-config-miss.
_RE_RUNTIME_DEP_NOT_FOUND = re.compile(
    r"Run-time dependency ([^\s:]+) found:\s*NO\b",
    re.IGNORECASE,
)

# Meson dependency(): "Dependency foo found: NO"
# Same routing rule as _RE_RUNTIME_DEP_NOT_FOUND.
_RE_DEP_NOT_FOUND = re.compile(
    r"^Dependency ([^\s:]+) found:\s*NO\b",
    re.IGNORECASE,
)

# Configure-time header probe: Has header "foo.h" : NO
_RE_HAS_HEADER = re.compile(
    r'[Hh]as header "([^"]+\.h[^"]*)" *: *NO',
)

# Configure-time header check: Checking for header "foo.h" : NO
_RE_CHECKING_HEADER = re.compile(
    r'[Cc]hecking for header "([^"]+\.h[^"]*)" *: *NO',
)

# Configure-time library probe: Library foo found: NO
_RE_LIBRARY_NOT_FOUND = re.compile(
    r"Library ([^\s:]+) found:\s*NO\b",
    re.IGNORECASE,
)

# Configure-time library check: Checking for library "foo" : NO
_RE_CHECKING_LIBRARY = re.compile(
    r'[Cc]hecking for library "([^"]+)" *: *NO',
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_misses(
    log_text: str,
    tool_dep_names: frozenset[str] | None = None,
) -> list[DepMiss]:
    """Parse meson setup output and return a deduplicated list of DepMiss.

    *tool_dep_names* must be the set of names present in TOOL_DEP_MAP
    (supplied by the caller from miss_mapper). When a ``found: NO``
    dependency line matches a name in this set, it is routed to
    ``tool-miss`` instead of ``pkg-config-miss``. This routing is
    deterministic: map membership is the signal. No inference is performed.

    Duplicate misses (same miss_type + name) are deduplicated; the first
    occurrence wins.
    """
    if tool_dep_names is None:
        tool_dep_names = frozenset()

    misses: list[DepMiss] = []
    seen: set[tuple[str, str]] = set()  # (miss_type, name)

    def _add(miss: DepMiss) -> None:
        key = (miss.miss_type, miss.name)
        if key not in seen:
            seen.add(key)
            misses.append(miss)

    for line in log_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # --- pkg-config: required-by variant (more specific — check first) ---
        m = _RE_PKGCFG_REQUIRED_BY.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="pkg-config-miss",
                name=m.group(1).strip().lower(),
                required_by=m.group(2).strip(),
                raw_line=stripped,
            ))
            continue

        # --- pkg-config: "No package 'foo' found" ---
        m = _RE_PKGCFG_NO_PACKAGE.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="pkg-config-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- pkg-config: "Package foo was not found in the pkg-config search path" ---
        m = _RE_PKGCFG_NOT_IN_PATH.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="pkg-config-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- find_program: "Program 'foo' not found or not executable" ---
        m = _RE_PROGRAM_NOT_FOUND.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="tool-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- find_program: "Program foo found: NO" ---
        m = _RE_PROGRAM_FOUND_NO.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="tool-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- Meson "Run-time dependency foo found: NO" ---
        # Route to tool-miss if name is in TOOL_DEP_MAP, else pkg-config-miss.
        m = _RE_RUNTIME_DEP_NOT_FOUND.search(stripped)
        if m:
            name = m.group(1).strip().lower()
            miss_type = "tool-miss" if name in tool_dep_names else "pkg-config-miss"
            _add(DepMiss(
                miss_type=miss_type,
                name=name,
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- Meson "Dependency foo found: NO" ---
        m = _RE_DEP_NOT_FOUND.search(stripped)
        if m:
            name = m.group(1).strip().lower()
            miss_type = "tool-miss" if name in tool_dep_names else "pkg-config-miss"
            _add(DepMiss(
                miss_type=miss_type,
                name=name,
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- configure-time header probe (meson setup only) ---
        m = _RE_HAS_HEADER.search(stripped) or _RE_CHECKING_HEADER.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="header-miss",
                name=m.group(1).strip(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- configure-time library probe (meson setup only) ---
        m = _RE_LIBRARY_NOT_FOUND.search(stripped) or _RE_CHECKING_LIBRARY.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="library-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

    return misses
