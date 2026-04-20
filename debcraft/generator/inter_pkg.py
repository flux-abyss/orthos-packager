"""Inter-package dependency synthesis and helper-script command detection.

This module contains three small, rule-based passes that run after the
package layout has been built from classified buckets:

1. synthesize_intra_deps  – decides which sibling packages the primary
   package should pull in (data, other-when-applicable).

2. dev_pkg_main_dep       – returns the versioned dep a -dev package
   should carry on its corresponding main package.

3. script_command_deps    – scans installed scripts in the stage tree for
   direct command invocations and returns matching Debian package names.

All three passes are intentionally minimal: explicit rules, no heavy
parsing, no speculative inference.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 1. Intra-package dependency synthesis
# ---------------------------------------------------------------------------

# Bucket names that the primary package should always depend on when present.
_PRIMARY_PULLS_ALWAYS = {"data"}

# Content patterns in the "other" bucket that indicate the primary package
# needs it at runtime.  We use simple suffix/keyword checks on the paths
# stored in the bucket's file list.
_OTHER_RUNTIME_SUFFIXES = (
    # Helper binaries
    ".so",
    # Shell / Python helpers that get exec'd by the main binary
    ".sh",
    ".py",
    # Core config files placed in /etc
    ".conf",
    ".ini",
    ".cfg",
    ".xml",
    ".desktop",
)
_OTHER_RUNTIME_PREFIXES = (
    "usr/bin/",
    "usr/sbin/",
    "usr/libexec/",
    "etc/",
)


def _other_is_runtime_needed(bucket_files: list[str]) -> bool:
    """Return True when the 'other' bucket looks like runtime support."""
    for f in bucket_files:
        rel = f.lstrip("/")
        if any(rel.startswith(p) for p in _OTHER_RUNTIME_PREFIXES):
            return True
        if any(rel.endswith(s) for s in _OTHER_RUNTIME_SUFFIXES):
            return True
    return False


def synthesize_intra_deps(
    app_name: str,
    non_empty: list[dict[str, Any]],
    primary_bucket: str | None,
) -> dict[str, list[str]]:
    """Return additional Depends per package name inferred from sibling buckets.

    Only the primary package gets intra-package deps (data, other).
    Secondary packages handled here via explicit rules only.

    Returns a mapping: package_name -> list of extra dep strings.
    """
    extras: dict[str, list[str]] = {}

    if primary_bucket is None:
        return extras

    primary_pkg = app_name  # primary bucket always maps to bare app_name

    for bucket in non_empty:
        bname = bucket["name"]
        if bname == primary_bucket:
            continue  # skip self

        sibling_pkg = f"{app_name}-{bname}"

        if bname in _PRIMARY_PULLS_ALWAYS:
            extras.setdefault(primary_pkg, []).append(sibling_pkg)
            continue

        if bname == "other" and _other_is_runtime_needed(bucket.get("files", [])):
            extras.setdefault(primary_pkg, []).append(sibling_pkg)

    return extras


# ---------------------------------------------------------------------------
# 2. Dev package dep semantics
# ---------------------------------------------------------------------------

def dev_pkg_main_dep(app_name: str) -> str:
    """Return the versioned Depends entry a -dev package carries on its main pkg.

    Uses ${binary:Version} so that upgrades stay in lockstep.
    """
    return f"{app_name} (= ${{binary:Version}})"


# ---------------------------------------------------------------------------
# 3. Installed-script command detection
# ---------------------------------------------------------------------------

# Static map: command name -> Debian package that provides it.
# Keep this small and intentional; only add entries when there is a clear
# need and the mapping is unambiguous.
SCRIPT_COMMAND_PKG_MAP: dict[str, str] = {
    "dbus-send":        "dbus-bin",
    "dbus-monitor":     "dbus-bin",
    "gdbus":            "libglib2.0-bin",
    "glib-compile-schemas": "libglib2.0-bin",
    "update-desktop-database": "desktop-file-utils",
    "gtk-update-icon-cache": "gtk-update-icon-cache",
    "update-mime-database": "shared-mime-info",
    "xdg-open":         "xdg-utils",
    "xdg-mime":         "xdg-utils",
    "xdg-icon-resource": "xdg-utils",
    "fc-cache":         "fontconfig",
    "gsettings":        "libglib2.0-bin",
    "systemctl":        "systemd",
    "loginctl":         "systemd",
}

# Script file suffixes we inspect.
_SCRIPT_SUFFIXES = {".sh", ".bash", ".py", ""}  # "" = no extension (shebangs)

# Magic bytes / shebang prefixes that indicate a script.
_SCRIPT_SHEBANGS = (b"#!/bin/sh", b"#!/bin/bash", b"#!/usr/bin/env",
                    b"#!/usr/bin/sh", b"#!/usr/bin/bash")


def _looks_like_script(path: Path) -> bool:
    """Return True when path is an executable text script."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode):
        return False
    if path.suffix not in _SCRIPT_SUFFIXES:
        return False
    try:
        header = path.read_bytes()[:64]
    except OSError:
        return False
    return any(header.startswith(s) for s in _SCRIPT_SHEBANGS)


def _commands_in_script(path: Path) -> set[str]:
    """Return command names directly invoked in *path* that are in our map."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()

    found: set[str] = set()
    for cmd in SCRIPT_COMMAND_PKG_MAP:
        # A word-boundary check: the command appears as a standalone token.
        # We accept it at the start of a word (after whitespace or pipe/semi).
        # This is intentionally simple – no AST, no shell parsing.
        if cmd in text:
            # Verify it is a token boundary (not a substring of another word).
            idx = text.find(cmd)
            while idx != -1:
                before = text[idx - 1] if idx > 0 else " "
                after_idx = idx + len(cmd)
                after = text[after_idx] if after_idx < len(text) else " "
                if (not before.isalnum() and before not in {"_", "-", "/"} and
                        not after.isalnum() and after not in {"_", "-", "/"}):
                    found.add(cmd)
                    break
                idx = text.find(cmd, idx + 1)
    return found


def script_command_deps(
    stage_dir: Path | None,
    pkg_files: list[str],
) -> list[tuple[str, str]]:
    """Return (package, reason) pairs for commands directly called in *pkg_files*.

    *pkg_files* is the list of install paths for a single binary package
    (relative to the stage root, possibly with leading '/').
    *stage_dir* is the staged install tree root.

    Returns a sorted, deduplicated list of (Debian package name, reason string)
    pairs.  Reason strings use the form:
        "script-command: <cmd> in <installed_path>"
    """
    if stage_dir is None or not stage_dir.is_dir():
        return []

    # pkg -> reason (first script/cmd pair that triggered it wins for dedup).
    found: dict[str, str] = {}

    for rel_path in pkg_files:
        installed = "/" + rel_path.lstrip("/")
        candidate = stage_dir / rel_path.lstrip("/")
        if not _looks_like_script(candidate):
            continue
        for cmd in _commands_in_script(candidate):
            pkg = SCRIPT_COMMAND_PKG_MAP.get(cmd)
            if pkg and pkg not in found:
                found[pkg] = f"script-command: {cmd} in {installed}"

    return sorted(found.items())
