"""Runtime smoke plan execution for Python packages.

E2A: skeleton + interfaces (RuntimeSmokeResult, run_runtime_smoke_plan).
E2B: implemented mechanics — artifact install + target execution inside a
     clean runtime chroot.

IMPORTANT: run_runtime_smoke_plan() executes runtime smoke plans when called by
           package flow.
"""

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from deb.discovery.chroot_env import ChrootEnv, ChrootEnvError
from deb.privileged.client import PrivilegedHelperError, chroot_exec
from deb.utils.log import error, info


@dataclass
class RuntimeSmokeResult:
    """Result of running a runtime smoke plan."""
    status: Literal["success", "failed", "skipped"]
    targets_total: int
    targets_passed: int
    targets_failed: int
    failures: list[dict[str, Any]] = field(default_factory=list)
    missing_dependencies: list[dict[str, Any]] = field(default_factory=list)
    log_path: Path | None = None


def _log(log_file: Path, text: str) -> None:
    """Append *text* to log_file, tolerating OSError."""
    try:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    except OSError as exc:
        error(f"smoke-runner: failed to write log: {exc}")


def _load_plan(smoke_plan_path: Path, log_file: Path) -> list[dict[str, Any]] | None:
    """Read and parse the smoke plan JSON.  Returns targets list or None on error."""
    try:
        raw = smoke_plan_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log(log_file, f"smoke-runner: cannot read smoke plan: {exc}\n")
        return None

    try:
        plan = json.loads(raw)
    except json.JSONDecodeError as exc:
        _log(log_file, f"smoke-runner: malformed smoke plan JSON: {exc}\n")
        return None

    targets = plan.get("targets", [])
    if not isinstance(targets, list):
        _log(log_file, "smoke-runner: smoke plan 'targets' is not a list\n")
        return None

    return targets


def run_runtime_smoke_plan(
    *,
    chroot_env: ChrootEnv,
    artifacts_dir: Path,
    smoke_plan_path: Path,
    log_file: Path,
) -> RuntimeSmokeResult:
    """Execute a runtime smoke plan inside an isolated runtime chroot.

    Caller responsibilities:
      - Provide a clean ChrootEnv that is NOT the build chroot.  Runtime smoke
        must be isolated from build-time packages so that missing runtime deps
        are NOT masked by accidentally-installed build deps.
      - The chroot must already exist (ensure_ready() called by the caller).
      - Provide a populated artifacts_dir containing the .deb files to test.
      - Provide a writable log_file path.

    Mount layout used internally:
      /orthos/source  -> artifacts_dir     (read-only; .deb files available here)
      /orthos/build   -> scratch_dir       (writable scratch for the chroot)
      /orthos/logs    -> log_file.parent   (writable log dir)

    Execution sequence:
      1. Parse the smoke plan; skip if empty.
      2. Collect .deb files from artifacts_dir; skip if none.
      3. Setup chroot mounts.
      4. apt-get update inside chroot.
      5. dpkg -i /orthos/source/*.deb  to install built artifacts.
      6. For each target command, run it inside the chroot and capture result.
      7. Tear down mounts in finally.
      8. Return RuntimeSmokeResult.

    Status semantics:
      - "skipped"  — no plan file, no targets, or no .deb artifacts found.
      - "failed"   — artifact install failed, or any target rc != 0.
      - "success"  — all targets returned rc == 0.
    """
    # ------------------------------------------------------------------
    # Step 1: parse smoke plan
    # ------------------------------------------------------------------
    if not smoke_plan_path.is_file():
        info("smoke-runner: no smoke plan found; skipping")
        return RuntimeSmokeResult(
            status="skipped",
            targets_total=0,
            targets_passed=0,
            targets_failed=0,
            log_path=log_file,
        )

    targets = _load_plan(smoke_plan_path, log_file)
    if targets is None:
        return RuntimeSmokeResult(
            status="skipped",
            targets_total=0,
            targets_passed=0,
            targets_failed=0,
            log_path=log_file,
        )

    if not targets:
        info("smoke-runner: smoke plan has no targets; skipping")
        _log(log_file, "smoke-runner: no targets in plan; skipped\n")
        return RuntimeSmokeResult(
            status="skipped",
            targets_total=0,
            targets_passed=0,
            targets_failed=0,
            log_path=log_file,
        )

    # ------------------------------------------------------------------
    # Step 2: locate .deb artifacts
    # ------------------------------------------------------------------
    debs = sorted(artifacts_dir.glob("*.deb"))
    if not debs:
        info("smoke-runner: no .deb artifacts found in artifacts_dir; skipping")
        _log(log_file, f"smoke-runner: no .deb artifacts in {artifacts_dir}; skipped\n")
        return RuntimeSmokeResult(
            status="skipped",
            targets_total=len(targets),
            targets_passed=0,
            targets_failed=0,
            failures=[{
                "reason": "no_artifacts",
                "message": f"No .deb files found in {artifacts_dir}",
            }],
            log_path=log_file,
        )

    _log(log_file, f"smoke-runner: found {len(debs)} artifact(s):\n")
    for d in debs:
        _log(log_file, f"  {d.name}\n")

    # ------------------------------------------------------------------
    # Step 3: setup mounts
    # Artifacts dir is bind-mounted as /orthos/source (read-only by convention).
    # A sibling scratch dir is used as /orthos/build; it is never used for
    # actual build output but is required by setup_mounts().
    # ------------------------------------------------------------------
    logs_dir = log_file.parent
    scratch_dir = logs_dir / "smoke-scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    mounts_up = False
    try:
        chroot_env.setup_mounts(
            source_repo=artifacts_dir,
            build_dir=scratch_dir,
            logs_dir=logs_dir,
        )
        mounts_up = True
    except ChrootEnvError as exc:
        msg = f"smoke-runner: failed to setup chroot mounts: {exc}"
        error(msg)
        _log(log_file, msg + "\n")
        return RuntimeSmokeResult(
            status="failed",
            targets_total=len(targets),
            targets_passed=0,
            targets_failed=0,
            failures=[{"reason": "mount_failed", "message": str(exc)}],
            log_path=log_file,
        )

    targets_passed = 0
    targets_failed = 0
    failures: list[dict[str, Any]] = []

    try:
        # ------------------------------------------------------------------
        # Step 4: apt-get update
        # ------------------------------------------------------------------
        _log(log_file, "\n# apt-get update\n")
        try:
            ok, apt_update_out = chroot_exec(
                chroot_env.root,
                ["bash", "-c", "apt-get update -qq"],
            )
        except PrivilegedHelperError as exc:
            msg = f"smoke-runner: apt-get update raised privileged helper error: {exc}"
            error(msg)
            _log(log_file, msg + "\n")
            return RuntimeSmokeResult(
                status="failed",
                targets_total=len(targets),
                targets_passed=0,
                targets_failed=0,
                failures=[{"reason": "apt_update_failed", "error": str(exc)}],
                log_path=log_file,
            )
        _log(log_file, apt_update_out)
        if not ok:
            msg = "smoke-runner: apt-get update failed inside chroot"
            error(msg)
            _log(log_file, msg + "\n")
            return RuntimeSmokeResult(
                status="failed",
                targets_total=len(targets),
                targets_passed=0,
                targets_failed=0,
                failures=[{"reason": "apt_update_failed", "output": apt_update_out}],
                log_path=log_file,
            )

        # ------------------------------------------------------------------
        # Step 5: install built .deb artifacts
        # The chroot sees them at /orthos/source/*.deb via the source bind-mount.
        # Use dpkg -i then apt-get install -f to resolve any missing deps.
        # ------------------------------------------------------------------
        _log(log_file, "\n# install artifacts\n")
        try:
            ok, dpkg_out = chroot_exec(
                chroot_env.root,
                ["bash", "-c", "dpkg -i /orthos/source/*.deb 2>&1 || true"],
            )
        except PrivilegedHelperError as exc:
            msg = f"smoke-runner: dpkg -i raised privileged helper error: {exc}"
            error(msg)
            _log(log_file, msg + "\n")
            return RuntimeSmokeResult(
                status="failed",
                targets_total=len(targets),
                targets_passed=0,
                targets_failed=0,
                failures=[{"reason": "artifact_install_failed", "error": str(exc)}],
                log_path=log_file,
            )
        _log(log_file, dpkg_out)

        try:
            ok2, fix_out = chroot_exec(
                chroot_env.root,
                ["bash", "-c", "apt-get install -f -y 2>&1"],
            )
        except PrivilegedHelperError as exc:
            msg = f"smoke-runner: apt-get install -f raised privileged helper error: {exc}"
            error(msg)
            _log(log_file, msg + "\n")
            return RuntimeSmokeResult(
                status="failed",
                targets_total=len(targets),
                targets_passed=0,
                targets_failed=0,
                failures=[{"reason": "artifact_install_failed", "error": str(exc)}],
                log_path=log_file,
            )
        _log(log_file, fix_out)

        if not ok2:
            msg = "smoke-runner: apt-get install -f failed; artifacts may be uninstallable"
            error(msg)
            _log(log_file, msg + "\n")
            return RuntimeSmokeResult(
                status="failed",
                targets_total=len(targets),
                targets_passed=0,
                targets_failed=0,
                failures=[{
                    "reason": "artifact_install_failed",
                    "dpkg_output": dpkg_out,
                    "apt_fix_output": fix_out,
                }],
                log_path=log_file,
            )

        # ------------------------------------------------------------------
        # Step 6: run each smoke target
        # ------------------------------------------------------------------
        for target in targets:
            raw_cmd = target.get("command", [])
            kind: str = target.get("kind", "unknown")
            name: str = target.get("name", "")

            if not raw_cmd:
                _log(log_file, f"\n# skip target (no command): {name}\n")
                continue

            if not isinstance(raw_cmd, list) or not all(isinstance(x, str) for x in raw_cmd):
                msg = f"smoke-runner: FAIL [{kind}] {name}: invalid command (not a list of strings)"
                error(msg)
                _log(log_file, f"\n# [{kind}] {name}: invalid_command\n")
                _log(log_file, msg + "\n")
                targets_failed += 1
                failures.append({
                    "reason": "invalid_command",
                    "kind": kind,
                    "name": name,
                    "command": raw_cmd
                })
                continue

            cmd: list[str] = raw_cmd
            shell_cmd = shlex.join(cmd)
            exec_cmd = ["bash", "-lc", shell_cmd]

            _log(log_file, f"\n# [{kind}] {name}: {shell_cmd}\n")

            try:
                ok_t, out_t = chroot_exec(chroot_env.root, exec_cmd)
            except PrivilegedHelperError as exc:
                msg = f"smoke-runner: FAIL [{kind}] {name}: chroot_exec raised: {exc}"
                error(msg)
                _log(log_file, msg + "\n")
                targets_failed += 1
                failures.append({
                    "reason": "chroot_exec_failed",
                    "kind": kind,
                    "name": name,
                    "command": cmd,
                    "error": str(exc),
                })
                continue

            _log(log_file, out_t)
            _log(log_file, f"# rc: {'0' if ok_t else 'nonzero'}\n")

            if ok_t:
                targets_passed += 1
                info(f"smoke-runner: PASS [{kind}] {name}")
            else:
                targets_failed += 1
                info(f"smoke-runner: FAIL [{kind}] {name}")
                failures.append({
                    "kind": kind,
                    "name": name,
                    "command": cmd,
                    "output": out_t,
                })

    finally:
        if mounts_up:
            chroot_env.teardown_mounts()

    # ------------------------------------------------------------------
    # Step 7: infer missing runtime dependencies from failures
    # ------------------------------------------------------------------
    from deb.runtime_smoke_failures import (
        infer_missing_runtime_dependencies,
        format_candidates_for_log,
    )
    missing_deps = infer_missing_runtime_dependencies(failures)
    missing_deps_as_dicts: list[dict[str, Any]] = [
        {
            "kind": c.kind,
            "name": c.name,
            "debian_package": c.debian_package,
            "evidence": c.evidence,
            "source": c.source,
        }
        for c in missing_deps
    ]
    if missing_deps:
        _log(log_file, "\n" + format_candidates_for_log(missing_deps))
        for c in missing_deps:
            pkg_str = c.debian_package if c.debian_package else "(unknown)"
            info(f"smoke-runner: missing dep candidate: {c.name} -> {pkg_str} [{c.kind}]")

    # ------------------------------------------------------------------
    # Step 8: build result
    # ------------------------------------------------------------------
    status: Literal["success", "failed", "skipped"] = (
        "success" if targets_failed == 0 else "failed"
    )
    info(
        f"smoke-runner: {status} - "
        f"{targets_passed}/{len(targets)} targets passed"
    )
    return RuntimeSmokeResult(
        status=status,
        targets_total=len(targets),
        targets_passed=targets_passed,
        targets_failed=targets_failed,
        failures=failures,
        missing_dependencies=missing_deps_as_dicts,
        log_path=log_file,
    )
