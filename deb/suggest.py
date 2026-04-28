"""Rule-based advisor over analyze-result.json.

Reads existing artifacts and produces suggest-result.json.
Does not modify any file in the repo or the generated debian/ directory.
Designed so a future AI backend can replace _apply_rules() without changing
the surrounding pipeline.
"""

import json
from pathlib import Path
from typing import Any

from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json

_ANALYZE_RESULT_FILE = "analyze-result.json"
_SUGGEST_RESULT_FILE = "suggest-result.json"


def _load_json(path: Path) -> dict[str, Any] | None:
    """Return parsed JSON from *path*, or None if the file is absent."""
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_analyze(path: Path) -> dict[str, Any]:
    """Read analyze-result.json; raise FileNotFoundError with a hint if absent."""
    if not path.exists():
        raise FileNotFoundError(f"analyze result not found: {path}\n"
                                f"Run 'orthos-packager analyze <repo>' first.")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _find_install_file(orthos: Path) -> str | None:
    """Return the path of the first .install file found in the generated debian/."""
    debian = orthos / "debian"
    if not debian.exists():
        return None
    for p in sorted(debian.glob("*.install")):
        return str(p)
    return None


# ---------------------------------------------------------------------------
# Rule table - one entry per failure category.
# Each entry is:
#   (suggestion_type, target_file_fn, suggested_change, next_step,
#    suggested_command, reasoning, confidence)
#
# target_file_fn receives orthos: Path and returns str | None.
# ---------------------------------------------------------------------------


def _target_control(orthos: Path) -> str | None:
    p = orthos / "debian" / "control"
    return str(p) if p.exists() else None


def _target_rules(orthos: Path) -> str | None:
    p = orthos / "debian" / "rules"
    return str(p) if p.exists() else None


def _target_install(orthos: Path) -> str | None:
    result = _find_install_file(orthos)
    if result:
        return result
    debian = orthos / "debian"
    return str(debian) if debian.exists() else None


def _target_null(_orthos: Path) -> str | None:
    return None


# Missing build dependency
MBD_SUGGESTED_CHANGE = (
    "The failing package likely belongs in the Build-Depends field "
    "of debian/control.")
MBD_NEXT_STEP = (
    "Inspect Build-Depends and compare against the unmet dependency "
    "shown in the log excerpt.")
MBD_REASONING = [
    "analyze classified the failure as missing_build_dependency",
    "dpkg-checkbuilddeps reports unmet build dependencies before compilation",
    "adding the missing package to Build-Depends is the standard fix",
]

# Missing install path
MIP_SUGGESTED_CHANGE = (
    "An install path listed in the .install file probably does not "
    "exist in the staged tree.")
MIP_NEXT_STEP = ("Compare .install entries against the staged install paths in "
                 ".orthos/<repo>/stage/.")
MIP_REASONING = [
    "analyze classified the failure as missing_install_path",
    "dh_install fails when a listed path cannot be found in the DESTDIR",
    "checking the staged tree against the .install file reveals the mismatch",
]

# Bad debian/control
BDC_SUGGESTED_CHANGE = (
    "The generated debian/control likely contains a malformed field "
    "or stanza.")
BDC_NEXT_STEP = ("Inspect the field or stanza referenced in the log excerpt "
                 "and cross-check with Debian Policy.")
BDC_REASONING = [
    "analyze classified the failure as bad_debian_control",
    "dpkg-source or dpkg-deb parse errors point to a structural control problem",
    "the generated control stanza should be checked for unknown fields",
]

# Bad debian/rules
BDR_SUGGESTED_CHANGE = (
    "debian/rules likely contains a bad override or a dh command "
    "that failed.")
BDR_NEXT_STEP = ("Inspect the failing dh_* or make line identified in the "
                 "log excerpt.")
BDR_REASONING = [
    "analyze classified the failure as bad_debian_rules",
    "debhelper failures surface as non-zero dh_* exits inside debian/rules",
    "checking the rules file and the specific override is the first step",
]

# Dpkg build failure
DBF_SUGGESTED_CHANGE = (
    "dpkg-buildpackage reported a fatal error that requires direct "
    "log inspection.")
DBF_NEXT_STEP = ("Inspect the first fatal dpkg line in the log excerpt and "
                 "consult the full build log.")
DBF_REASONING = [
    "analyze classified the failure as dpkg_build_failure",
    "dpkg errors can have many root causes not detectable without full context",
    "the build log is the primary source of truth",
]

# Unknown failure
UNKNOWN_SUGGESTED_CHANGE = (
    "No reliable automatic suggestion is available for this "
    "failure.")
UNKNOWN_NEXT_STEP = (
    "Inspect analyze-result.json and the full build.log directly.")
UNKNOWN_REASONING = [
    "no failure category matched the known patterns",
    "manual log inspection is required",
]

# Upstream test / validation failure
UTF_SUGGESTED_CHANGE = (
    "An upstream test or validation step failed during the package build.")
UTF_NEXT_STEP = (
    "Inspect the failing test or validation command in the build log, "
    "then check the upstream test output for the specific assertion.")
UTF_REASONING = [
    "analyze classified the failure as upstream_test_failure",
    "dh_auto_test or meson test exited non-zero during package build",
    "the failure is in upstream code, not in the packaging files",
]

_RULES: dict[str, dict[str, Any]] = {
    "missing_build_dependency": {
        "suggestion_type": "control_edit",
        "target_file_fn": _target_control,
        "suggested_change": MBD_SUGGESTED_CHANGE,
        "next_step": MBD_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/debian/control",
        "reasoning": MBD_REASONING,
        "confidence": "high",
    },
    "missing_install_path": {
        "suggestion_type": "install_file_edit",
        "target_file_fn": _target_install,
        "suggested_change": MIP_SUGGESTED_CHANGE,
        "next_step": MIP_NEXT_STEP,
        "suggested_command": "find .orthos/<repo>/stage -type f | sort",
        "reasoning": MIP_REASONING,
        "confidence": "high",
    },
    "bad_debian_control": {
        "suggestion_type": "control_edit",
        "target_file_fn": _target_control,
        "suggested_change": BDC_SUGGESTED_CHANGE,
        "next_step": BDC_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/debian/control",
        "reasoning": BDC_REASONING,
        "confidence": "medium",
    },
    "bad_debian_rules": {
        "suggestion_type": "rules_edit",
        "target_file_fn": _target_rules,
        "suggested_change": BDR_SUGGESTED_CHANGE,
        "next_step": BDR_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/debian/rules",
        "reasoning": BDR_REASONING,
        "confidence": "medium",
    },
    "dpkg_build_failure": {
        "suggestion_type": "manual_investigation",
        "target_file_fn": _target_null,
        "suggested_change": DBF_SUGGESTED_CHANGE,
        "next_step": DBF_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/logs/build.log",
        "reasoning": DBF_REASONING,
        "confidence": "low",
    },
    "upstream_test_failure": {
        "suggestion_type": "manual_investigation",
        "target_file_fn": _target_null,
        "suggested_change": UTF_SUGGESTED_CHANGE,
        "next_step": UTF_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/logs/build.log",
        "reasoning": UTF_REASONING,
        "confidence": "medium",
    },
    "unknown": {
        "suggestion_type": "manual_investigation",
        "target_file_fn": _target_null,
        "suggested_change": UNKNOWN_SUGGESTED_CHANGE,
        "next_step": UNKNOWN_NEXT_STEP,
        "suggested_command": "cat .orthos/<repo>/analyze-result.json",
        "reasoning": UNKNOWN_REASONING,
        "confidence": "low",
    },
}


def _apply_rules(
    orthos: Path,
    category: str,
) -> dict[str, Any]:
    """Return a suggestion dict for *category* using the static rule table.

    This function is the seam point for a future AI backend: callers need not
    change - replace or wrap this function to add model-backed suggestions.
    """
    rule = _RULES.get(category, _RULES["unknown"])
    target = rule["target_file_fn"](orthos)

    return {
        "confidence": rule["confidence"],
        "next_step": rule["next_step"],
        "reasoning": rule["reasoning"],
        "suggested_change": rule["suggested_change"],
        "suggested_command": rule["suggested_command"],
        "suggestion_type": rule["suggestion_type"],
        "target_file": target,
    }


def suggest(meta: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    """Read analyze-result.json and emit suggest-result.json for *meta*.

    Returns (exit_code, result_dict, suggest_file_path).  Always exits 0.
    """
    repo = Path(meta["repo_path"])
    orthos = orthos_dir(repo)

    analyze = _load_analyze(orthos / _ANALYZE_RESULT_FILE)
    success: bool = bool(analyze.get("success", False))
    category: str | None = analyze.get("category")

    if success:
        result: dict[str, Any] = {
            "category": None,
            "confidence": "high",
            "next_step": None,
            "reasoning": ["build succeeded; no suggestion needed"],
            "suggested_change": None,
            "suggested_command": None,
            "success": True,
            "suggestion_type": None,
            "target_file": None,
        }
    else:
        suggestion = _apply_rules(orthos, category or "unknown")
        result = {"success": False, "category": category, **suggestion}

    ensure_dir(orthos)
    suggest_file = orthos / _SUGGEST_RESULT_FILE
    write_json(suggest_file, result)
    return 0, result, str(suggest_file)
