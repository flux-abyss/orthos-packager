"""Runtime dependency convergence state management.

This module manages the persistent state file that accumulates runtime
dependency candidates discovered by smoke test failures across package runs.

State file: .orthos/<project>/runtime-dep-convergence.json

Schema:
  {
    "pass_number": 1,
    "candidates": [
      {
        "kind":           "python-module" | "command" | "gi-namespace",
        "name":           "<bare name>",
        "debian_package": "<deb pkg>" | null,
        "evidence":       "<error text snippet>",
        "source":         "runtime-smoke"
      }
    ],
    "extra_depends": ["<deb pkg>", ...]
  }

``extra_depends`` contains only the non-null ``debian_package`` values
from ``candidates``, deduplicated and sorted, ready to be injected into
generated debian/control.

This module does NOT trigger rebuilds or re-run smoke tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deb.runtime_smoke_runner import RuntimeSmokeResult

_STATE_FILE = "runtime-dep-convergence.json"


# ---------------------------------------------------------------------------
# Schema helper
# ---------------------------------------------------------------------------

def _state_path(orthos_dir: Path) -> Path:
    return orthos_dir / _STATE_FILE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_runtime_convergence_depends(orthos_dir: Path) -> list[str]:
    """Return the accumulated extra_depends list from the convergence state file.

    Returns an empty list when no state file exists or the file is unreadable.
    The caller should treat this list as additional Depends entries and merge
    them into the package's ``extra_depends`` before debian/control generation.
    """
    path = _state_path(orthos_dir)
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    deps = data.get("extra_depends", [])
    if not isinstance(deps, list):
        return []
    return [d for d in deps if isinstance(d, str) and d]


def write_runtime_convergence_state(
    orthos_dir: Path,
    result: "RuntimeSmokeResult",
    pass_num: int,
) -> None:
    """Persist runtime smoke missing-dependency candidates to the state file.

    Merges *result.missing_dependencies* into any previously saved candidates,
    deduplicates by (kind, name), and recomputes ``extra_depends`` from all
    candidates with non-null ``debian_package`` values.

    Args:
        orthos_dir: The .orthos/<project>/ workspace directory.
        result:     The RuntimeSmokeResult from the most recent smoke run.
        pass_num:   The current convergence pass number (1-based).

    Raises nothing.  Errors are silently swallowed because convergence state
    is advisory; a failure here must not crash the package pipeline.
    """
    path = _state_path(orthos_dir)

    # Load existing state if present.
    existing_candidates: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            existing_candidates = data.get("candidates", [])
            if not isinstance(existing_candidates, list):
                existing_candidates = []
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing_candidates = []

    # Build a deduplicated candidates list.  Existing entries come first so
    # that their evidence is preserved; new entries are appended.
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []

    for c in existing_candidates:
        if not isinstance(c, dict):
            continue
        key = (str(c.get("kind", "")), str(c.get("name", "")))
        if key not in seen:
            seen.add(key)
            merged.append(c)

    for c in result.missing_dependencies:
        if not isinstance(c, dict):
            continue
        key = (str(c.get("kind", "")), str(c.get("name", "")))
        if key not in seen:
            seen.add(key)
            merged.append(dict(c))

    # Derive extra_depends: only candidates with a non-null debian_package.
    # Deduplicate while preserving first-seen order.
    seen_pkgs: set[str] = set()
    extra_depends: list[str] = []
    for c in merged:
        pkg = c.get("debian_package")
        if isinstance(pkg, str) and pkg and pkg not in seen_pkgs:
            seen_pkgs.add(pkg)
            extra_depends.append(pkg)

    state: dict[str, Any] = {
        "pass_number": pass_num,
        "candidates": merged,
        "extra_depends": sorted(extra_depends),
    }

    try:
        path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError:
        # Advisory state; failure is non-fatal.
        pass
