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
import subprocess
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


def _find_header_for_symbol(
    symbol: str,
    include_roots: list[str],
) -> str | None:
    """Return the path of the first .h file in *include_roots* that contains
    *symbol* as a literal string, or None if not found.

    Complements _symbol_in_headers: same walk, same text search, but
    returns the path instead of a bool so the caller can resolve ownership.
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
                    return str(fpath)
    return None


def _find_header_by_keyword(
    keyword: str,
    include_roots: list[str],
) -> str | None:
    """Return the first .h file path whose path contains *keyword* (case-insensitive).

    Also tries *keyword* with underscores replaced by hyphens, since many C
    libraries (e.g. EFL) use hyphenated directory names ('ecore-x-1/') while
    their symbols use underscores ('ecore_x_...').

    Single-keyword primitive. Callers that need specificity should call this
    repeatedly with decreasing-specificity keywords and take the first result.
    """
    kw = keyword.lower()
    kw_hyph = kw.replace("_", "-")
    candidates = {kw, kw_hyph}

    for root_str in include_roots:
        root = Path(root_str)
        if not root.is_dir():
            continue
        for dirpath, _dirs, filenames in os.walk(root):
            dir_lower = dirpath.lower()
            files_lower = "\n".join(filenames).lower()
            if not any(c in dir_lower or c in files_lower for c in candidates):
                continue
            for fname in filenames:
                if not fname.endswith(".h"):
                    continue
                fname_lower = fname.lower()
                if any(c in dir_lower or c in fname_lower for c in candidates):
                    return str(Path(dirpath) / fname)
    return None


def _descending_prefixes(symbol: str, min_len: int = 3) -> list[str]:
    """Return underscore-joined left-to-right prefixes of *symbol*, longest first.

    Only prefixes whose total assembled length is at least *min_len* characters
    are included (avoids matching on trivially short tokens like 'e' or 'x').

    Examples
    --------
    >>> _descending_prefixes('ecore_x_io_error_display_still_there_get')
    ['ecore_x_io_error_display_still_there', ..., 'ecore_x', 'ecore']

    >>> _descending_prefixes('evas_object_event_rects_set')
    ['evas_object_event_rects', 'evas_object_event', 'evas_object', 'evas']
    """
    parts = symbol.split("_")
    prefixes: list[str] = []
    for i in range(len(parts) - 1, 0, -1):   # longest slice first
        prefix = "_".join(parts[:i])
        if len(prefix) >= min_len:
            prefixes.append(prefix)
    return prefixes


def _dpkg_s_host(header_path: str) -> str | None:
    """Resolve the Debian package owning *header_path* via host dpkg -S.

    Returns a normalised package name, or None. Used when no runner is
    available (standalone stage, host mode).
    """
    try:
        result = subprocess.run(
            ["dpkg", "-S", header_path],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    line = result.stdout.strip().splitlines()[0]
    if ":" in line:
        pkg = line.split(":")[0].strip()
        return pkg.lower() if pkg else None
    return None


def infer_symbol_provider(
    symbol: str,
    include_roots: list[str],
    runner: object = None,
) -> dict | None:
    """Infer the Debian package that provides the header defining *symbol*.

    Strategy
    --------
    1. Search include_roots for a header that **contains** *symbol* literally.
       This covers the case where an older version of the header is installed
       (the symbol exists somewhere in the file even if the exact API variant
       differs).
    2. If not found (symbol is entirely new to this distro version), extract
       the library keyword from the symbol name (first underscore-separated
       component) and find any header whose *path* contains that keyword.
       The installed header file exists even if it lacks the new symbol.
    3. Resolve the owning package:
       - If *runner* has a 'dpkg_search_path' method, delegate to it.
       - Otherwise call dpkg -S directly on the host.
    4. Return a dict {symbol, header, package} or None if resolution fails.

    Parameters
    ----------
    symbol:
        The missing symbol name extracted from compiler output.
    include_roots:
        Filesystem paths to search (host or chroot /usr/include, etc.).
    runner:
        Optional RunnerProtocol instance. Passed as 'object' to avoid a
        circular import; duck-typed at call time.
    """
    # Step 1: find a header that literally contains the symbol.
    header_path = _find_header_for_symbol(symbol, include_roots)

    # Step 2: fallback — try descending-specificity underscore prefixes so we
    # match the most relevant header path rather than the first alphabetical hit.
    # e.g. ecore_x_io_... tries 'ecore_x' before falling back to 'ecore'.
    if header_path is None:
        for prefix in _descending_prefixes(symbol):
            header_path = _find_header_by_keyword(prefix, include_roots)
            if header_path is not None:
                break

    if header_path is None:
        return None

    # Step 3: resolve package owner.
    package: str | None = None
    if runner is not None and hasattr(runner, "dpkg_search_path"):
        package = runner.dpkg_search_path(header_path)  # type: ignore[union-attr]
    if package is None:
        package = _dpkg_s_host(header_path)

    if package is None:
        return None

    return {
        "symbol": symbol,
        "header": header_path,
        "package": package,
    }


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
