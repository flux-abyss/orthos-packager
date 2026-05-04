"""Meson staging backend: setup -> compile -> install into a DESTDIR."""

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deb.expert.compat import evaluate_compile_failure, infer_symbol_provider
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.shell import run_logged

if TYPE_CHECKING:
    from deb.discovery.runner import RunnerProtocol

_RESULT_FILE = "stage-result.json"
StageResult = dict[str, Any]

# System paths that must appear first so Meson finds system Python, not venv.
_SYSTEM_PATH_PREPEND = ["/usr/bin", "/bin"]


def _clean_env() -> dict[str, str]:
    """Return a copy of os.environ with the active venv stripped from PATH.

    Ensures Meson subprocesses discover system Python rather than any
    interpreter embedded in the caller's virtual environment.
    """
    env = dict(os.environ)
    venv = env.get("VIRTUAL_ENV", "")
    venv_bin = os.path.join(venv, "bin") if venv else ""

    parts = env.get("PATH", "").split(os.pathsep)
    parts = [p for p in parts if p and p != venv_bin]
    for d in reversed(_SYSTEM_PATH_PREPEND):
        if d not in parts:
            parts.insert(0, d)

    env["PATH"] = os.pathsep.join(parts)
    return env


# Known host include roots used by the system compiler.
# stage() runs meson compile on the host, so expert analysis of compile
# failures must use host headers - not chroot headers from prior convergence.
_HOST_INCLUDE_CANDIDATES: list[str] = [
    "/usr/include",
    "/usr/include/x86_64-linux-gnu",
]


def _stage_include_roots() -> list[str]:
    """Return host include paths that actually exist on this system.

    Used by the expert compat rule to confirm whether a missing symbol is
    genuinely absent from the installed headers the compiler can see.
    Only paths that exist are returned; missing directories are silently
    skipped so the rule degrades gracefully on non-standard layouts.
    """
    return [p for p in _HOST_INCLUDE_CANDIDATES if Path(p).is_dir()]


def _next_step_strategy(verdicts: list[dict]) -> dict | None:
    """Return a structured strategy dict if verdicts indicate a mode switch.

    Returns None when the verdicts do not require a strategy change (i.e.
    ordinary dependency resolution should continue).
    """
    ids = {v.get("rule_id") for v in verdicts}
    if "source_too_new_for_target_api" in ids:
        return {
            "next_mode": "compatibility_search",
            "compatibility_strategy": "prefer_tag_or_release",
            "compatibility_reason": "source_too_new_for_target_api",
        }
    return None


def _infer_provider_from_verdicts(
    verdicts: list[dict],
    include_roots: list[str],
    runner: object,
) -> dict | None:
    """Extract absent symbol names from verdicts and infer a provider package.

    Scans the evidence lines of any 'source_too_new_for_target_api' verdict
    for the first absent symbol, then delegates to infer_symbol_provider.
    Returns the first successful provider dict, or None.
    """
    # Pull the first C identifier from a compiler diagnostic line.
    _sym_pat = re.compile(r"""['"‘’“”]([A-Za-z_][A-Za-z0-9_]*)['"‘’“”]""")

    for verdict in verdicts:
        if verdict.get("rule_id") != "source_too_new_for_target_api":
            continue
        for evidence_line in verdict.get("evidence", []):
            m = _sym_pat.search(evidence_line)
            if not m:
                continue
            symbol = m.group(1)
            provider = infer_symbol_provider(symbol, include_roots, runner=runner)
            if provider:
                return provider
    return None


def _query_target_version(
    runner: "RunnerProtocol",
    package: str,
    pkgconfig_module: str,
) -> dict | None:
    """Query the target environment for installed version data.

    Returns a dict suitable for inclusion as 'target_version_info' in the
    stage result, or None if neither query returns useful data.

    Both queries are best-effort and silently return None on failure so that
    a missing or misconfigured environment does not abort analysis.
    """
    pkg_ver = runner.pkg_query_version(package)
    pc_ver = runner.pkgconfig_modversion(pkgconfig_module)
    if pkg_ver is None and pc_ver is None:
        return None
    return {
        "package": package,
        "package_version": pkg_ver,
        "pkgconfig_module": pkgconfig_module,
        "pkgconfig_version": pc_ver,
    }


def stage(meta: dict[str, Any], runner: "RunnerProtocol | None" = None) -> tuple[int, StageResult]:
    """Run the full Meson staging flow for the repo described by *meta*.

    Directories created under <repo>/.orthos/:
        build/ - Meson build tree
        stage/ - DESTDIR install root
        logs/ - combined build log

    Returns:
        A tuple of (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    orthos = orthos_dir(repo)

    build_dir = orthos / "build"
    stage_dir = orthos / "stage"
    logs_dir = orthos / "logs"

    for directory in (build_dir, stage_dir, logs_dir):
        ensure_dir(directory)

    log_file = logs_dir / "stage.log"
    log_file.write_text("", encoding="utf-8")

    success = True
    failure_step: str | None = None

    clean = _clean_env()

    meson_options: dict[str, str] = meta.get("meson_options") or {}
    meson_option_flags = [f"-D{k}={v}" for k, v in sorted(meson_options.items())]

    ok, _ = run_logged(
        [
            "meson",
            "setup",
            str(build_dir),
            str(repo),
            "--prefix=/usr",
            "--sysconfdir=/etc",
            "--localstatedir=/var",
            "--libdir=lib/x86_64-linux-gnu",
            *meson_option_flags,
        ],
        log_file=log_file,
        env=clean,
    )
    if not ok:
        success = False
        failure_step = "meson setup"

    compile_output = ""
    if success:
        ok, compile_output = run_logged(
            ["meson", "compile", "-C", str(build_dir)],
            log_file=log_file,
            env=clean,
        )
        if not ok:
            success = False
            failure_step = "meson compile"

    if success:
        install_env = {**clean, "DESTDIR": str(stage_dir)}
        ok, _ = run_logged(
            ["meson", "install", "-C", str(build_dir)],
            log_file=log_file,
            env=install_env,
        )
        if not ok:
            success = False
            failure_step = "meson install"

    result: StageResult = {
        "build_dir": str(build_dir),
        "log_file": str(log_file),
        "project_name": meta.get("project_name"),
        "repo_path": str(repo),
        "stage_dir": str(stage_dir),
        "success": success,
        "version": meta.get("version"),
    }
    if failure_step is not None:
        result["failure_step"] = failure_step

    # Run expert rules when compile failed and we have output to evaluate.
    # stage() compiles on the host, so use host include roots regardless of
    # whether a .orthos/chroot/ directory exists from prior convergence work.
    expert_verdicts: list[dict] = []
    if failure_step == "meson compile" and compile_output:
        verdicts = evaluate_compile_failure(compile_output, _stage_include_roots())
        expert_verdicts = [v.as_dict() for v in verdicts]

    if expert_verdicts:
        result["expert_verdicts"] = expert_verdicts
        # Translate verdicts into a structured pipeline strategy recommendation.
        # Currently only one verdict drives a strategy change; extend here as
        # new rules are added.
        strategy = _next_step_strategy(expert_verdicts)
        if strategy:
            result.update(strategy)
            # When switching to compatibility search, report what the target
            # environment actually has installed.  HostRunner is imported here
            # (not at module level) because runner.py imports _clean_env from
            # this module, which would create a circular import at load time.
            from deb.discovery.runner import HostRunner  # noqa: PLC0415
            active_runner: "RunnerProtocol" = runner if runner is not None else HostRunner()

            # Infer provider from the first absent symbol in the verdict.
            # The verdict evidence lines are raw compiler log lines; we need
            # the symbol names, which are stored in the verdict summary.
            # Use the absent symbols captured during this call instead.
            provider = _infer_provider_from_verdicts(
                expert_verdicts, _stage_include_roots(), active_runner
            )

            if provider:
                package = provider["package"]
                # Derive a pkg-config module name from the package name:
                # strip the leading 'lib' and trailing '-dev' if present.
                pc_module = package
                if pc_module.startswith("lib"):
                    pc_module = pc_module[3:]
                if pc_module.endswith("-dev"):
                    pc_module = pc_module[:-4]
                result["symbol_provider"] = provider
            else:
                # No provider inferred - skip version reporting rather than
                # guessing a hardcoded package name.
                package = ""
                pc_module = ""

            if package:
                version_info = _query_target_version(
                    active_runner,
                    package=package,
                    pkgconfig_module=pc_module,
                )
                if version_info:
                    result["target_version_info"] = version_info

    write_json(orthos / _RESULT_FILE, result)
    return (0 if success else 1), result

