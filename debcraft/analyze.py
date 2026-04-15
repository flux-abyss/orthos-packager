"""Post-build analysis: read build-result.json + build.log and emit a summary."""

import json
from pathlib import Path
from typing import Any

from debcraft.utils.fs import ensure_dir, write_json

_BUILD_RESULT_FILE = "build-result.json"
_LOG_FILE = "logs/build.log"
_ANALYZE_RESULT_FILE = "analyze-result.json"

# Ordered list of (category, list_of_trigger_substrings).
# First matching category wins.
_CATEGORIES: list[tuple[str, list[str]]] = [
    ("missing_build_dependency", [
        "No package '",
        "dependency problems",
        "unmet build dependencies",
        "Unmet build dependencies",
    ]),
    ("missing_install_path", [
        "No such file or directory",
        "cannot stat",
        "cannot find",
    ]),
    ("bad_debian_control", [
        "control file has",
        "unknown field",
        "malformed",
        "parse error in",
        "error in Depends",
    ]),
    # upstream_test_failure must precede bad_debian_rules so
    # "dh_auto_test: error" is not swallowed by the generic "dh_" trigger.
    ("upstream_test_failure", [
        "dh_auto_test: error",
        "meson test --verbose returned exit code",
        "Validate desktop file FAIL",
        "desktop-file-validate",
    ]),
    ("bad_debian_rules", [
        "dh_",
        "override_dh_",
        "make: *** [debian/rules]",
        "debian/rules:",
    ]),
    ("dpkg_build_failure", [
        "dpkg-buildpackage: error",
        "dpkg-source: error",
        "dpkg-deb: error",
    ]),
]


def _orthos_dir(repo_path: Path) -> Path:
    """Mirror the layout used by all earlier steps."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def _load_build_result(path: Path) -> dict[str, Any]:
    """Read build-result.json; raise FileNotFoundError if absent."""
    if not path.exists():
        raise FileNotFoundError(f"build result not found: {path}\n"
                                f"Run 'orthos-packager build <repo>' first.")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _load_log(path: Path) -> list[str]:
    """Return lines from build.log; return [] if the file is missing."""
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# Informational prefixes that are noisy and should not count as diagnostics.
_INFO_PREFIXES = ("dpkg-buildpackage: info:",)

# Substrings that strongly indicate a real failure line.
_FAIL_KEYWORDS = (
    "error:",
    "Error:",
    "ERROR",
    "FAIL",
    "failed",
    "Failed",
    "FAILED",
    "fatal",
    "Fatal",
    "returned exit code",
    "subprocess returned exit status",
    "dh_auto_test: error",
    "desktop-file-validate",
    "Validate desktop file FAIL",
    "No such",
    "unmet",
    "cannot",
    "make: ***",
)


def _relevant_lines(lines: list[str]) -> list[str]:
    """Return real failure lines from the log - max 5.

    Skips informational lines (e.g. dpkg-buildpackage: info:) that are
    not diagnostic.
    """
    hits: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if any(stripped.startswith(p) for p in _INFO_PREFIXES):
            continue
        if any(kw in stripped for kw in _FAIL_KEYWORDS):
            hits.append(stripped)
        if len(hits) == 5:
            break
    return hits


def _classify(lines: list[str]) -> str:
    """Return the first matching failure category, or 'unknown'."""
    for line in lines:
        for category, triggers in _CATEGORIES:
            if any(t in line for t in triggers):
                return category
    return "unknown"


def _make_summary(success: bool, category: str | None,
                  excerpt: list[str]) -> str:
    """Return a ≤2-sentence human summary."""
    if success:
        return "Build completed successfully."

    first = excerpt[0] if excerpt else "No diagnostic output found."
    descriptions: dict[str, str] = {
        "missing_build_dependency":
            "A required build dependency was not found.",
        "missing_install_path":
            "The build could not find a file or path during install.",
        "bad_debian_control":
            "The debian/control file contains a parse error or unknown field.",
        "bad_debian_rules":
            "The debian/rules file caused a debhelper failure.",
        "upstream_test_failure":
            "The build failed during upstream tests or validation.",
        "dpkg_build_failure":
            "dpkg-buildpackage reported a fatal error.",
        "unknown":
            "The build failed for an unrecognised reason.",
    }
    base = descriptions.get(category or "unknown", descriptions["unknown"])
    return f"{base} First diagnostic: {first}"


def analyze(meta: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
    """Read build outputs for *meta* and write analyze-result.json.

    Returns (exit_code, result_dict, analyze_file_path).  Always exits 0 -
    analysis itself does not fail; the build result's success flag is reported,
    not re-raised.
    """
    repo = Path(meta["repo_path"])
    orthos = _orthos_dir(repo)

    build_result = _load_build_result(orthos / _BUILD_RESULT_FILE)
    log_lines = _load_log(orthos / _LOG_FILE)

    success: bool = bool(build_result.get("success", False))

    if success:
        category: str | None = None
        excerpt: list[str] = []
    else:
        excerpt = _relevant_lines(log_lines)
        category = _classify(excerpt or log_lines)

    summary = _make_summary(success, category, excerpt)

    result: dict[str, Any] = {
        "category": category,
        "log_excerpt": excerpt,
        "success": success,
        "summary": summary,
    }

    ensure_dir(orthos)
    analyze_file = orthos / _ANALYZE_RESULT_FILE
    write_json(analyze_file, result)
    return 0, result, str(analyze_file)
