"""Expert rule: source_too_new_for_target_api.

Detects when a compile failure is caused by the source code referencing
symbols, types, or struct members that are absent from the target distro's
installed headers — rather than from a missing Debian package.

This is specifically NOT a missing-dependency problem. The package that
provides the relevant headers is already installed; the installed version
simply does not expose the symbol the source requires. The source was written
against a newer API than the target distro ships.

Public API
----------
evaluate_compile_failure(compiler_output, include_roots) -> list[ExpertVerdict]

The caller supplies:
  compiler_output  — combined stdout+stderr from the failed compile step
  include_roots    — filesystem paths to search for installed headers
                     (e.g. chroot /usr/include, or host /usr/include)

The function returns zero or more ExpertVerdict instances. Zero means the
rule did not find sufficient evidence to fire.

Rule trigger conditions (ALL must hold)
----------------------------------------
1. compiler_output contains one or more compile-time API mismatch patterns:
     - "implicit declaration of function 'X'"
     - "unknown type name 'X'"
     - "'X' undeclared"
     - "has no member named 'X'"
     - "error: 'X' was not declared in this scope"
2. At least one of the extracted symbol names is NOT found in any header
   file under the provided include_roots.
3. Absence is confirmed by literal grep of header text, not by inference.

Limitation notes
----------------
- Only .h files are searched. Inline namespaces or generated headers not
  present in include_roots will cause false positives. Confidence is capped
  at 0.85 to reflect this.
- The rule does not walk git history or suggest a specific commit. That is
  a separate, later rule.
- Network lookups are never performed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from debcraft.expert.models import ExpertVerdict

_RULE_ID = "source_too_new_for_target_api"

# Quote characters seen in compiler diagnostics (ASCII and Unicode).
_Q = r"""['"‘’“”]"""

_MISMATCH_PATTERNS: list[re.Pattern[str]] = [
    # GCC/Clang: implicit declaration
    re.compile(
        rf"implicit declaration of function {_Q}([A-Za-z_][A-Za-z0-9_]*){_Q}"
    ),
    # GCC/Clang: unknown type name
    re.compile(
        rf"unknown type name {_Q}([A-Za-z_][A-Za-z0-9_]*){_Q}"
    ),
    # GCC/Clang: struct member access on wrong type
    re.compile(
        rf"has no member named {_Q}([A-Za-z_][A-Za-z0-9_]*){_Q}"
    ),
    # GCC/Clang: undeclared identifier
    re.compile(
        rf"{_Q}([A-Za-z_][A-Za-z0-9_]*){_Q} undeclared"
    ),
    # GCC/Clang: not declared in this scope
    re.compile(
        rf"{_Q}([A-Za-z_][A-Za-z0-9_]*){_Q} was not declared in this scope"
    ),
    # GCC/Clang: 'X' does not name a type
    re.compile(
        rf"{_Q}([A-Za-z_][A-Za-z0-9_]*){_Q} does not name a type"
    ),
]


def _extract_missing_symbols(compiler_output: str) -> list[tuple[str, str]]:
    """Return [(symbol_name, raw_log_line), ...] for each API mismatch found.

    Deduplicates by symbol name (first occurrence wins). Only lines that
    match one of the known mismatch patterns are returned.
    """
    seen: set[str] = set()
    results: list[tuple[str, str]] = []
    for line in compiler_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pat in _MISMATCH_PATTERNS:
            m = pat.search(stripped)
            if m:
                sym = m.group(1)
                if sym not in seen:
                    seen.add(sym)
                    results.append((sym, stripped))
                break  # one pattern match per line is enough
    return results


def _symbol_in_headers(symbol: str, include_roots: list[str]) -> bool:
    """Return True if *symbol* appears as a literal string in any .h file
    under any of the provided *include_roots*.

    This is a plain text search, not a C parser. It will find function
    declarations, typedef names, and macro definitions that contain the
    exact symbol string. It will not handle obfuscated or macro-generated
    declarations, which is an acceptable limitation at this confidence level.
    """
    for root_str in include_roots:
        root = Path(root_str)
        if not root.is_dir():
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            for fname in filenames:
                if not fname.endswith(".h"):
                    continue
                fpath = Path(dirpath) / fname
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if symbol in text:
                    return True
    return False


def evaluate_compile_failure(
    compiler_output: str,
    include_roots: list[str],
) -> list[ExpertVerdict]:
    """Evaluate *compiler_output* for API mismatch evidence.

    Parameters
    ----------
    compiler_output:
        Combined stdout+stderr from a failed compile step (meson compile
        output, ninja output, or raw gcc/clang invocation output).
    include_roots:
        List of filesystem paths to search for installed header files.
        For host builds: typically ["/usr/include"].
        For chroot builds: typically ["<chroot_root>/usr/include"].
        Paths that do not exist are silently skipped.

    Returns
    -------
    A list of ExpertVerdict instances. Empty list means the rule did not
    fire. Currently at most one verdict is returned per call (the rule is
    binary: either the evidence threshold is met or it is not).
    """
    if not compiler_output.strip():
        return []
    if not include_roots:
        return []

    # Step 1: extract all symbols that triggered compile-time API mismatch patterns.
    candidates = _extract_missing_symbols(compiler_output)
    if not candidates:
        return []

    # Step 2: check which of those symbols are absent from the installed headers.
    absent: list[tuple[str, str]] = []  # (symbol, raw_log_line)
    for sym, raw_line in candidates:
        if not _symbol_in_headers(sym, include_roots):
            absent.append((sym, raw_line))

    if not absent:
        # All mismatch symbols were found in target headers — likely a different
        # class of error (e.g. wrong usage of an existing API). Rule does not fire.
        return []

    # Step 3: emit a single verdict summarising all absent symbols.
    absent_names = [sym for sym, _ in absent]
    evidence_lines = [raw for _, raw in absent]

    # Confidence: 0.85 cap because header text search cannot parse all C
    # constructs (generated headers, macro-generated names, etc.).
    confidence = min(0.85, 0.5 + 0.1 * len(absent))

    verdict = ExpertVerdict(
        rule_id=_RULE_ID,
        category="compatibility",
        confidence=round(confidence, 2),
        summary=(
            f"Source references {len(absent_names)} symbol(s) absent from "
            "target distro headers: "
            + ", ".join(absent_names[:5])
            + (" ..." if len(absent_names) > 5 else "")
            + ". Source may be newer than the target distro API."
        ),
        evidence=evidence_lines[:10],  # cap evidence to keep JSON readable
        suggested_action=(
            "This is likely a source/API compatibility problem, not a missing "
            "Debian package. Recommended steps: (1) try an older release or "
            "tag of the source that targets this distro's API version; "
            "(2) locate the commit that introduced usage of the absent symbol "
            "and consider whether a patch or backport is feasible; "
            "(3) do not continue dependency resolution — the missing symbol "
            "is not in any available package."
        ),
    )
    return [verdict]
