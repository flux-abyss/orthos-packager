"""Classify concrete missing requirements from meson setup output.

Operates only on meson setup stdout+stderr. Does not classify compile-phase
or link-phase output - those are later extension points.

Classification is fully deterministic: fixed substring and regex patterns.
No scoring, no fuzzy inference.

Miss types in scope:
  pkg-config-miss  - missing pkg-config module
  tool-miss        - missing program or tool-type dependency
  header-miss      - missing header (configure-time probe only)
  library-miss     - missing library (configure-time probe only)
  source-issue     - upstream/source issue (e.g. rustc version mismatch)

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

    miss_type: str           # "pkg-config-miss" | "tool-miss" | "header-miss" | "library-miss" | "source-issue"
    name: str                # concrete missing name: e.g. "lua51", "wayland-scanner", "foo.h"
    required_by: str | None  # e.g. "edje" from pkg-config required-by output
    raw_line: str            # original log line, preserved verbatim


# ---------------------------------------------------------------------------
# Patterns - meson setup stdout/stderr
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

# Meson compiler probe: "ERROR: Could not execute Vala compiler: valac"
# Generalised to any "Could not execute <label>: <executable>" line.
# The executable name is the last colon-delimited token (stripped).
_RE_COULD_NOT_EXECUTE = re.compile(
    r"Could not execute [^:]+:\s*(\S+)",
    re.IGNORECASE,
)

# Meson run_command / custom_target failure: shell command returned exit 127
# ("command not found").  Captured so we can scan the command text for known
# tools and emit a tool-miss without accepting arbitrary shell words.
# Example:
#   ERROR: Command `/usr/bin/bash -lc 'cd /path && cargo build'` failed with status 127.
_RE_FAILED_STATUS_127 = re.compile(
    r"ERROR: Command `([^`]+)` failed with status 127",
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

# Cargo rustc version incompatibility.
# Cargo prints this when the resolved dependency graph requires a newer rustc
# than the one installed.  It is a source/upstream issue, not an installable
# package dependency.
# Examples:
#   rustc 1.85.0 is not supported by the following packages:
#   icu_collections@2.2.0 requires rustc 1.86
_RE_CARGO_RUSTC_VERSION = re.compile(
    r"rustc\s+\d[\d.]+\s+is not supported by the following packages"
    r"|requires\s+rustc\s+(\d[\d.]+)",
    re.IGNORECASE,
)

# Meson installs a file from a hardcoded source-tree Cargo target path while
# CARGO_TARGET_DIR has redirected Cargo output elsewhere.
# Example:
#   ERROR: File tools/html2rss/target/release/html2rss does not exist.
# The pattern matches any path of the form tools/<name>/target/release/<file>.
_RE_MESON_CARGO_HARDCODED_TARGET = re.compile(
    r"ERROR: File ([^\s]+/target/release/[^\s]+) does not exist",
    re.IGNORECASE,
)

_CARGO_IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", ".orthos", "debian", "build", "_build",
    "target", ".cargo", "vendor", "node_modules",
    "subprojects",
})


def _cargo_toml_is_ignored(rel: Path) -> bool:
    """Return True if any component of *rel* is in _CARGO_IGNORE_DIRS."""
    for part in rel.parts:
        if part in _CARGO_IGNORE_DIRS:
            return True
    return False


def check_cargo_lock_misses(repo: Path) -> list["DepMiss"]:
    """Return source-issue DepMiss for every Cargo.toml without a Cargo.lock.

    Rules:
    - Only runs when Cargo.toml files exist under *repo* (non-ignored dirs).
    - For each relevant Cargo.toml the following satisfy the check:
        a) A Cargo.lock sibling in the same directory.
        b) A Cargo.lock at the repository root.
    - Cargo.toml files under ignored dirs are skipped entirely.
    - No cargo is invoked; no files are created or modified.
    """
    root_lock = (repo / "Cargo.lock").is_file()
    misses: list[DepMiss] = []
    seen: set[str] = set()

    for toml in repo.rglob("Cargo.toml"):
        try:
            rel = toml.relative_to(repo)
        except ValueError:
            continue
        if _cargo_toml_is_ignored(rel):
            continue
        # Check for a sibling Cargo.lock or root Cargo.lock.
        if (toml.parent / "Cargo.lock").is_file() or root_lock:
            continue
        key = str(rel)
        if key in seen:
            continue
        seen.add(key)
        misses.append(DepMiss(
            miss_type="source-issue",
            name=f"cargo-missing-lock:{rel}",
            required_by=None,
            raw_line=f"Cargo.toml without Cargo.lock: {rel}",
        ))
    return misses


def source_issue_diagnostic(miss_name: str) -> str:
    """Return a human-readable message for a source-issue miss name.

    Source issues are upstream/source-side problems that Orthos cannot resolve
    by installing Debian packages.  The diagnostic is logged for the operator.
    """
    if miss_name.startswith("cargo-rustc-version:"):
        required = miss_name.split(":", 1)[1]
        if required and required != "unknown":
            return (
                f"Cargo dependency graph requires rustc >= {required}, but the "
                "target toolchain does not satisfy this. Provide a compatible "
                "Cargo.lock (e.g. pin older crate versions) or target a newer "
                "Rust toolchain."
            )
        return (
            "Cargo dependency graph requires a newer rustc than the target "
            "provides. Provide a compatible Cargo.lock or target a newer Rust "
            "toolchain."
        )
    if miss_name.startswith("cargo-hardcoded-target:"):
        path = miss_name.split(":", 1)[1]
        return (
            f"Meson expects Cargo output at {path!r} (source-tree path), but "
            "CARGO_TARGET_DIR redirected Cargo output elsewhere. Upstream Meson "
            "should respect CARGO_TARGET_DIR when installing helper binaries."
        )
    if miss_name.startswith("cargo-missing-lock:"):
        toml_path = miss_name.split(":", 1)[1]
        return (
            f"Rust crate has Cargo.toml but no Cargo.lock ({toml_path}). "
            "Debian packaging needs a reproducible dependency graph. "
            "Commit a compatible Cargo.lock or provide a source patch."
        )
    return f"source-side issue: {miss_name}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_misses(
    log_text: str,
    tool_dep_names: frozenset[str] | None = None,
) -> list[DepMiss]:
    """Parse meson setup output and return a deduplicated list of DepMiss.

    *tool_dep_names* must be the set of names present in TOOL_DEP_MAP
    (supplied by the caller from miss_mapper). When a 'found: NO'
    dependency line matches a name in this set, it is routed to
    'tool-miss' instead of 'pkg-config-miss'. This routing is
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

        # --- pkg-config: required-by variant (more specific - check first) ---
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

        # --- compiler probe: "Could not execute <label>: <executable>" ---
        m = _RE_COULD_NOT_EXECUTE.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="tool-miss",
                name=m.group(1).strip().lower(),
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- shell command exit 127 ("command not found") ---
        # Only emit a tool-miss when the command text contains a known tool
        # name from TOOL_DEP_MAP.  This prevents classifying arbitrary shell
        # words as tool misses.
        m = _RE_FAILED_STATUS_127.search(stripped)
        if m:
            cmd_text = m.group(1).lower()
            for tool in tool_dep_names:
                # Match whole-word to avoid false positives (e.g. "cargo" in
                # a path component vs the bare executable name).
                if re.search(r"(?<![\w/-])" + re.escape(tool) + r"(?![\w])", cmd_text):
                    _add(DepMiss(
                        miss_type="tool-miss",
                        name=tool,
                        required_by=None,
                        raw_line=stripped,
                    ))
                    break  # one miss per line
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

        # --- Cargo rustc version mismatch (source-side issue) ---
        # Detect lines like "rustc X.Y.Z is not supported by..." or
        # "crate@version requires rustc X.Y" from cargo output embedded in
        # the meson log.  Represented as a source-issue so convergence treats
        # it as unresolvable and surfaces a clear diagnostic, never as a
        # Debian package dependency.
        m = _RE_CARGO_RUSTC_VERSION.search(stripped)
        if m:
            version = (m.group(1) or "unknown").strip()
            _add(DepMiss(
                miss_type="source-issue",
                name=f"cargo-rustc-version:{version}",
                required_by=None,
                raw_line=stripped,
            ))
            continue

        # --- Meson hardcoded Cargo target path (source-side issue) ---
        # Meson checks a hardcoded source-tree path tools/<x>/target/release/<y>
        # while CARGO_TARGET_DIR was set.  The binary exists but in the
        # redirected location.  This is an upstream build-system issue.
        m = _RE_MESON_CARGO_HARDCODED_TARGET.search(stripped)
        if m:
            _add(DepMiss(
                miss_type="source-issue",
                name=f"cargo-hardcoded-target:{m.group(1).strip()}",
                required_by=None,
                raw_line=stripped,
            ))
            continue

    # Post-process: Reduce noise in Cargo rustc-version diagnostics.
    # If we have any concrete versions (e.g. 'cargo-rustc-version:1.86'),
    # drop any 'cargo-rustc-version:unknown' to keep the diagnostic clear.
    has_concrete_rustc = any(
        m.miss_type == "source-issue" and 
        m.name.startswith("cargo-rustc-version:") and 
        not m.name.endswith(":unknown")
        for m in misses
    )
    if has_concrete_rustc:
        misses = [
            m for m in misses
            if not (
                m.miss_type == "source-issue" and 
                m.name == "cargo-rustc-version:unknown"
            )
        ]

    return misses
