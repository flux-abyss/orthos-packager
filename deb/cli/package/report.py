"""Package report generator for orthos.

Reads existing Orthos JSON result files and emits:
  .orthos/<project>/package-report.txt   (human-readable)
  .orthos/<project>/package-report.json  (machine-readable)

Designed to be called on both success and failure paths.
No external dependencies beyond the standard library.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    """Format seconds as a compact human-readable duration."""
    if seconds < 0:
        return "?"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    if m < 60:
        return f"{m}m{s:02d}s"
    h = m // 60
    m2 = m % 60
    return f"{h}h{m2:02d}m{s:02d}s"


def _fmt_size(n_bytes: int) -> str:
    """Format byte count as a compact human-readable size."""
    if n_bytes < 0:
        return "?"
    for unit, threshold in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n_bytes >= threshold:
            return f"{n_bytes / threshold:.1f} {unit}"
    return f"{n_bytes} B"


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning an empty dict on any error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _artifact_size(path_str: str) -> int:
    """Return byte size of an artifact file, or 0 on error."""
    try:
        return Path(path_str).stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _collect(orthos: Path, timings: dict[str, float] | None = None) -> dict[str, Any]:
    """Read all available result JSON files and assemble a flat report dict."""
    meta        = _read_json(orthos / "package-meta.json")
    convergence = _read_json(orthos / "convergence-result.json")
    stage       = _read_json(orthos / "stage-result.json")
    inventory   = _read_json(orthos / "install-inventory.json")
    plan        = _read_json(orthos / "package-plan.json")
    gen_result  = _read_json(orthos / "generate-result.json")

    # Artifacts: prefer artifacts/ dir listing over JSON pointers.
    artifacts_dir = orthos / "artifacts"
    if artifacts_dir.is_dir():
        artifacts = sorted(str(p) for p in artifacts_dir.glob("*.deb"))
    else:
        artifacts = []

    # Source issues from convergence unresolved_misses
    source_issues: list[str] = []
    for miss in convergence.get("unresolved_misses", []):
        if miss.get("miss_type") == "source-issue":
            from deb.discovery.miss_classifier import source_issue_diagnostic
            source_issues.append(source_issue_diagnostic(miss.get("name", "")))

    # Package buckets from plan
    buckets: list[dict[str, Any]] = plan.get("package_buckets", [])

    # Artifact sizes
    artifact_info = [
        {"path": p, "size": _artifact_size(p)} for p in artifacts
    ]
    total_artifact_bytes = sum(a["size"] for a in artifact_info)

    return {
        # identity
        "project_name":      meta.get("project_name") or orthos.name,
        "version":           meta.get("version") or "",
        "repo_path":         meta.get("repo_path") or "",
        # upstream metadata
        "upstream_name":     meta.get("upstream_name") or "",
        "upstream_contact":  meta.get("upstream_contact") or "",
        "source_url":        meta.get("source_url") or "",
        "license":           meta.get("license") or "",
        "upstream_copyright":meta.get("upstream_copyright") or "",
        # convergence
        "convergence_success":    convergence.get("success", False),
        "convergence_passes":     convergence.get("passes", 0),
        "convergence_mode":       convergence.get("runner_mode", "chroot"),
        "convergence_stalled":    convergence.get("stalled", False),
        "convergence_stall_reason": convergence.get("stall_reason"),
        "convergence_provenance": convergence.get("provenance", []),
        "convergence_large_batch_warnings": convergence.get("large_batch_warnings", []),
        "convergence_log_file":   convergence.get("log_file", ""),
        # source issues
        "source_issues":     source_issues,
        # inventory
        "inventory_total":   inventory.get("total_files", 0),
        "inventory_by_kind": inventory.get("counts_by_kind", {}),
        # plan / layout
        "binary_packages":   gen_result.get("binary_packages") or plan.get("binary_packages", []),
        "build_depends":     gen_result.get("build_depends") or "",
        "build_depends_source": gen_result.get("build_depends_source") or "",
        "package_buckets":   buckets,
        "debian_dir":        gen_result.get("debian_dir") or "",
        # artifacts
        "artifacts":         artifact_info,
        "total_artifact_bytes": total_artifact_bytes,
        # paths
        "logs_dir":          str(orthos / "logs"),
        "orthos_dir":        str(orthos),
        # timings (caller-supplied)
        "timings":           timings or {},
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _section(lines: list[str], title: str) -> None:
    lines.append("")
    lines.append(title)
    lines.append("-" * len(title))


def _render_txt(data: dict[str, Any], status: str, elapsed: float) -> str:
    """Render a human-readable ASCII report."""
    L: list[str] = []

    # ---- Header verdict ----
    L.append("Orthos Package Report")
    L.append("=" * 60)
    L.append(f"Status:      {status}")
    name = data["project_name"]
    ver  = data["version"]
    L.append(f"Project:     {name}" + (f" {ver}" if ver else ""))
    if data["repo_path"]:
        L.append(f"Repo:        {data['repo_path']}")
    L.append(f"Mode:        {data['convergence_mode']}")
    L.append(f"Elapsed:     {_fmt_duration(elapsed)}")

    pkgs = data["binary_packages"]
    L.append(f"Packages:    {len(pkgs)}")

    art = data["artifacts"]
    if art:
        L.append(f"Artifacts:   {len(art)}, {_fmt_size(data['total_artifact_bytes'])}")
    else:
        L.append("Artifacts:   (none)")

    orthos = Path(data["orthos_dir"])
    L.append(f"Report:      {orthos / 'package-report.txt'}")
    L.append(f"Logs:        {data['logs_dir']}")

    # ---- Source issues (prominent on failure) ----
    if data["source_issues"]:
        _section(L, "Source Issues")
        for issue in data["source_issues"]:
            L.append(f"  [!] {issue}")

    # ---- Upstream metadata ----
    _section(L, "Upstream Metadata")
    fields = [
        ("Name",      data["upstream_name"]),
        ("Contact",   data["upstream_contact"]),
        ("Source",    data["source_url"]),
        ("License",   data["license"]),
        ("Copyright", data["upstream_copyright"]),
    ]
    for label, val in fields:
        if val:
            L.append(f"  {label+':':<12}{val}")

    # ---- Convergence ----
    _section(L, "Convergence")
    conv_status = "success" if data["convergence_success"] else (
        f"stalled ({data['convergence_stall_reason']})" if data["convergence_stalled"]
        else "incomplete"
    )
    L.append(f"  Status:      {conv_status}")
    L.append(f"  Passes:      {data['convergence_passes']}")

    prov = data["convergence_provenance"]
    if prov:
        # Group by pass_number
        by_pass: dict[int, list[str]] = {}
        for entry in prov:
            p = entry.get("pass_number", 0)
            by_pass.setdefault(p, []).append(entry.get("package", "?"))
        for pass_num in sorted(by_pass):
            pkgs_in_pass = ", ".join(by_pass[pass_num])
            L.append(f"  Pass {pass_num}:      {pkgs_in_pass}")

    for w in data["convergence_large_batch_warnings"]:
        L.append(f"  WARNING: {w}")

    # ---- Inventory ----
    if data["inventory_total"]:
        _section(L, "Inventory")
        L.append(f"  Total files: {data['inventory_total']}")
        for kind, count in sorted(data["inventory_by_kind"].items()):
            L.append(f"  {kind+':':<26}{count}")

    # ---- Package layout ----
    if pkgs:
        _section(L, "Package Layout")
        for pkg in pkgs:
            L.append(f"  {pkg}")

    # ---- Generated debian/ ----
    deb_dir = data["debian_dir"]
    if deb_dir:
        _section(L, "Generated debian/")
        L.append(f"  Path:        {deb_dir}")
        if data["build_depends"]:
            bd = data["build_depends"]
            # Wrap long build-depends at 72 chars
            L.append(f"  Build-Deps:  {bd[:72]}")
            if len(bd) > 72:
                L.append(f"               {bd[72:]}")
        if data["build_depends_source"]:
            L.append(f"  BD source:   {data['build_depends_source']}")

    # ---- Artifacts ----
    if art:
        _section(L, "Artifacts")
        for a in art:
            fname = Path(a["path"]).name
            L.append(f"  {fname}  ({_fmt_size(a['size'])})")
        L.append(f"  Total: {_fmt_size(data['total_artifact_bytes'])}")

    # ---- Phase timings ----
    timings = data.get("timings") or {}
    if timings:
        _section(L, "Phase Timings")
        for phase, secs in timings.items():
            L.append(f"  {phase+':':<20}{_fmt_duration(secs)}")

    # ---- Next steps ----
    _section(L, "Next Steps")
    if deb_dir:
        L.append(f"  Inspect generated debian/:  ls {deb_dir}")
    if art:
        art_dir = Path(data["orthos_dir"]) / "artifacts"
        L.append(f"  Install artifacts:  sudo apt install {art_dir}/*.deb")
    L.append(f"  View logs:  ls {data['logs_dir']}")

    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_package_report(
    orthos: Path,
    status: str,
    elapsed: float,
    timings: dict[str, float] | None = None,
) -> tuple[Path, Path]:
    """Collect data, render, and write package-report.txt and package-report.json.

    Args:
        orthos:   The .orthos/<project> directory.
        status:   "OK" or "FAILED".
        elapsed:  Total elapsed seconds for the package run.
        timings:  Optional dict of phase_name -> seconds.

    Returns:
        (txt_path, json_path)
    """
    data = _collect(orthos, timings=timings)
    data["status"] = status
    data["elapsed"] = elapsed

    txt = _render_txt(data, status, elapsed)

    txt_path  = orthos / "package-report.txt"
    json_path = orthos / "package-report.json"

    txt_path.write_text(txt, encoding="utf-8")

    # JSON: strip artifact path info to just name+size, drop full prov list noise.
    json_data = {k: v for k, v in data.items() if k != "convergence_provenance"}
    json_path.write_text(
        json.dumps(json_data, indent=2, default=str),
        encoding="utf-8",
    )

    return txt_path, json_path


def print_verdict(
    status: str,
    project_name: str,
    version: str,
    mode: str,
    elapsed: float,
    pkg_count: int,
    artifact_count: int,
    total_artifact_bytes: int,
    orthos: Path,
    source_issues: list[str] | None = None,
) -> None:
    """Print a compact terminal verdict at the end of a package run."""
    sep = "=" * 46
    print("")
    print("Orthos Verdict")
    print(sep)
    print(f"Status:      {status}")
    label = project_name + (f" {version}" if version else "")
    print(f"Project:     {label}")
    print(f"Mode:        {mode}")
    print(f"Elapsed:     {_fmt_duration(elapsed)}")
    print(f"Packages:    {pkg_count}")
    if artifact_count:
        print(f"Artifacts:   {artifact_count}, {_fmt_size(total_artifact_bytes)}")
    else:
        print("Artifacts:   (none)")
    print(f"Report:      {orthos / 'package-report.txt'}")
    print(f"Logs:        {orthos / 'logs'}")
    if source_issues:
        print("")
        print("Source Issues:")
        for issue in source_issues:
            print(f"  [!] {issue}")
    print(sep)
    print("")
