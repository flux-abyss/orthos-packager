"""Convergence loop for Orthos dependency discovery.

Implements the convergence loop:
  Pass 1 - static Meson hint seed: resolve scan_meson_dependencies() output
            to packages, install them in the runner's environment as a batch.
  Pass 2+ - meson setup interrogation: run meson setup via the runner, classify
             misses from output, map to packages, batch install, repeat.

The loop exits when:
  - meson setup exits 0            (success)
  - all misses are unresolvable    (stall_reason="unresolved")
  - no new packages can be found   (stall_reason="no-new-packages")
  - _MAX_CONVERGENCE_PASSES passes are exhausted

Runner modes:
  The loop accepts any RunnerProtocol implementation. The runner determines
  where commands execute (host or chroot). All execution differences are
  encapsulated in the runner; this module contains no host/chroot logic.

  runner_mode is recorded in ConvergenceResult and convergence-result.json
  so every run is permanently auditable.

SUCCESS SEMANTICS:
  ConvergenceResult.success=True means meson setup exited 0. It does NOT
  imply compile success, link success, or dpkg-buildpackage completeness.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deb.build_deps import (
    BODHI_BUILD_DEP_MAP,
    scan_meson_dependencies,
)
from deb.discovery.miss_classifier import DepMiss, classify_misses
from deb.discovery.miss_mapper import map_miss_to_package, tool_dep_names
from deb.discovery.runner import HostRunner, RunnerProtocol
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import error, info

if TYPE_CHECKING:
    pass

# Maximum number of meson setup interrogation passes (pass 2 onwards).
# Pass 1 (static hint seed) does not count against this limit.
_MAX_CONVERGENCE_PASSES: int = 8

# If a single convergence pass resolves more than this many packages, log
# a warning. Does not abort - used to surface mapping explosions.
_LARGE_BATCH_THRESHOLD: int = 25

_RESULT_FILE = "convergence-result.json"

# meson setup flags used in all convergence passes.
_MESON_FLAGS: list[str] = [
    "--prefix=/usr",
    "--sysconfdir=/etc",
    "--localstatedir=/var",
    "--libdir=lib/x86_64-linux-gnu",
    "--wipe",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceEntry:
    """Recorded justification for a single installed package."""

    package: str
    miss_type: str           # "static-meson-hint" | "pkg-config-miss" | "tool-miss" |
    #                          "header-miss" | "library-miss"
    miss_name: str           # concrete name that triggered the install (e.g. "lua51")
    required_by: str | None
    pass_number: int         # 1 = static hint seed, 2+ = convergence pass N


@dataclass
class ConvergenceResult:
    """Outcome of a full convergence run.

    success=True means meson setup exited 0. It does NOT imply compile
    success, link success, or that dpkg-buildpackage dependencies are met.

    runner_mode: "host" or "chroot" - written to convergence-result.json.

    isolation_scope: reflects what is actually isolated in this run.
      "convergence-only" when running in chroot mode - the meson setup
      interrogation and package installs happen inside the chroot, but the
      later stage/build pipeline still runs on the host.
      "host" when running in host mode (no isolation at all).

    install_failed=True when apt install returned nonzero inside the loop.
      This is a fatal condition: the caller must stop smoke immediately.
      Distinct from stalled=True which is advisory (the stage step handles it).

    stall_reason values:
      "no-new-packages" - misses classified/mapped, but all resolved
                          packages were already installed; no progress.
      "unresolved"      - misses classified, but map_miss_to_package
                          returned None for all; no candidates exist.
      None              - not stalled (loop succeeded or max passes hit).
    """

    success: bool
    passes: int
    runner_mode: str = "host"
    isolation_scope: str = "host"
    install_failed: bool = False
    provenance: list[ProvenanceEntry] = field(default_factory=list)
    stalled: bool = False
    stall_reason: str | None = None
    unresolved_misses: list[DepMiss] = field(default_factory=list)
    large_batch_warnings: list[str] = field(default_factory=list)
    log_file: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_seed_packages(
    repo: Path,
    runner: RunnerProtocol,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return (package, meson_name) pairs from the static Meson hint layer.

    Resolution uses only the curated BODHI_BUILD_DEP_MAP.  If a Meson
    dependency name is not present in the map it is silently skipped; the
    convergence loop will surface it as a concrete miss during meson setup
    interrogation (Pass 2+) where the path-anchored pkgconfig_file_search
    applies the appropriate precision check.

    The previous runner.apt_search_dev(name) fallback has been removed.
    That call performed a broad apt-cache name-pattern search
    (e.g. 'lzma.*-dev') which accepted any package whose Debian name
    contained the dependency word, regardless of whether the package
    actually shipped the corresponding pkg-config module.  For evisum this
    caused golang-github-kjk-lzma-dev and similar unrelated packages to be
    seeded and installed.
    """
    names = scan_meson_dependencies(repo)
    if not names:
        return [], []

    seen_pkgs: set[str] = set()
    pairs: list[tuple[str, str]] = []
    unresolved: list[str] = []

    for meson_name in names:
        normalized = meson_name.strip().lower()

        # Curated map only - same in all environments, no network required.
        pkg: str | None = BODHI_BUILD_DEP_MAP.get(normalized)
        if not pkg:
            continue

        normalized_pkg = pkg.strip().lower()
        
        target_pkg: str | None = normalized_pkg
        if not runner.pkg_query_exists(target_pkg):
            if target_pkg.startswith("lib") and target_pkg.endswith("-dev"):
                base_name = target_pkg[3:-4]
                fallback = f"lib{base_name}-all-dev"
                if runner.pkg_query_exists(fallback):
                    target_pkg = fallback
                else:
                    target_pkg = None
            else:
                target_pkg = None

        if not target_pkg:
            if meson_name not in unresolved:
                unresolved.append(meson_name)
            continue

        if target_pkg not in seen_pkgs:
            seen_pkgs.add(target_pkg)
            pairs.append((target_pkg, meson_name))

    return pairs, unresolved



def _write_result(orthos: Path, result: ConvergenceResult) -> None:
    """Serialize ConvergenceResult to convergence-result.json."""
    data: dict[str, Any] = {
        "success": result.success,
        "passes": result.passes,
        "runner_mode": result.runner_mode,
        "isolation_scope": result.isolation_scope,
        "install_failed": result.install_failed,
        "stalled": result.stalled,
        "stall_reason": result.stall_reason,
        "large_batch_warnings": result.large_batch_warnings,
        "log_file": result.log_file,
        "provenance": [asdict(p) for p in result.provenance],
        "unresolved_misses": [
            {
                "miss_type": m.miss_type,
                "name": m.name,
                "required_by": m.required_by,
                "raw_line": m.raw_line,
            }
            for m in result.unresolved_misses
        ],
    }
    write_json(orthos / _RESULT_FILE, data)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_convergence_loop(
    repo: Path,
    runner: RunnerProtocol | None = None,
) -> ConvergenceResult:
    """Run the convergence loop for *repo* using *runner*.

    When *runner* is None, a HostRunner is constructed (backward-compatible
    behavior - identical to the pre-isolation host-based round).

    Pass 1: Resolve static Meson hints, install seed packages via runner.
    Pass 2+: Run meson setup via runner, classify misses, map to packages,
             batch install via runner, retry until success or stall.

    Returns ConvergenceResult regardless of outcome.
    convergence-result.json is always written. Does not raise.
    """
    if runner is None:
        runner = HostRunner()

    orthos = orthos_dir(repo)
    logs_dir = orthos / "logs"
    build_dir = orthos / "build"
    ensure_dir(logs_dir)
    ensure_dir(build_dir)

    _tool_names = tool_dep_names()
    isolation_scope = "convergence-only" if runner.mode == "chroot" else "host"
    result = ConvergenceResult(
        success=False,
        passes=0,
        runner_mode=runner.mode,
        isolation_scope=isolation_scope,
    )

    info(f"convergence: mode = {runner.mode}")
    if runner.mode == "chroot":
        info(
            "convergence: isolation_scope = convergence-only - "
            "meson setup and package installs run in chroot; "
            "stage/build pipeline runs on host in this round"
        )

    # ------------------------------------------------------------------
    # Pass 1 - static Meson hint seed
    # ------------------------------------------------------------------
    info("convergence: pass 1 - static Meson hint seed")
    seed_log = logs_dir / "convergence-pass-1.log"
    seed_log.write_text("", encoding="utf-8")

    seed_pairs, unresolved_seeds = _resolve_seed_packages(repo, runner)
    if seed_pairs:
        info(
            f"convergence: pass 1 - {len(seed_pairs)} hint candidate(s): "
            f"{', '.join(pkg for pkg, _ in seed_pairs)}"
        )
    else:
        info("convergence: pass 1 - no static hint candidates resolved")

    for meson_name in unresolved_seeds:
        result.unresolved_misses.append(DepMiss(
            miss_type="static-meson-hint",
            name=meson_name,
            required_by=None,
            raw_line=f"seed hint for {meson_name}",
        ))
        info(f"  unresolvable: static-meson-hint: {meson_name}")

    # Record provenance for ALL resolved seed packages (installed or not).
    # Provenance is the audit trail; installation status is tracked separately.
    for pkg, meson_name in seed_pairs:
        result.provenance.append(ProvenanceEntry(
            package=pkg,
            miss_type="static-meson-hint",
            miss_name=meson_name,
            required_by=None,
            pass_number=1,
        ))

    seed_to_install = sorted({
        pkg for pkg, _ in seed_pairs
        if not runner.is_pkg_installed(pkg)
    })

    if seed_to_install:
        info(
            f"convergence: pass 1 - installing {len(seed_to_install)} "
            f"package(s): {', '.join(seed_to_install)}"
        )
        rc = runner.apt_install(seed_to_install)
        if rc != 0:
            error("convergence: apt install failed for seed batch (fatal)")
            result.install_failed = True
            result.log_file = str(seed_log)
            _write_result(orthos, result)
            return result
    else:
        info("convergence: pass 1 - all seed packages already installed")

    result.passes = 1

    # ------------------------------------------------------------------
    # Passes 2..N - meson setup interrogation
    # ------------------------------------------------------------------
    last_log_file = seed_log

    for pass_num in range(2, _MAX_CONVERGENCE_PASSES + 2):
        log_file = logs_dir / f"convergence-pass-{pass_num}.log"
        log_file.write_text("", encoding="utf-8")
        last_log_file = log_file

        meson_cmd = [
            "meson", "setup",
            runner.meson_source_path(repo),
            runner.meson_build_path(build_dir),
          *_MESON_FLAGS,
        ]

        info(f"convergence: pass {pass_num} - running meson setup "
             f"({runner.mode})")
        success, output = runner.run_command(meson_cmd, log_file)

        if success:
            info(f"convergence: pass {pass_num} - meson setup succeeded")
            result.success = True
            result.passes = pass_num
            result.log_file = str(log_file)
            _write_result(orthos, result)
            return result

        info(
            f"convergence: pass {pass_num} - meson setup failed, "
            "classifying misses"
        )

        misses = classify_misses(output, tool_dep_names=_tool_names)
        if not misses:
            info(
                f"convergence: pass {pass_num} - no classifiable misses "
                "in output; stalling"
            )
            result.stalled = True
            result.stall_reason = "no-new-packages"
            result.passes = pass_num
            break

        info(f"convergence: pass {pass_num} - {len(misses)} miss(es) found")

        # Map misses → packages; first resolved per package name wins.
        candidates: dict[str, DepMiss] = {}
        unresolved_this_pass: list[DepMiss] = []

        for miss in misses:
            pkg = map_miss_to_package(miss, runner=runner)
            if pkg is None:
                unresolved_this_pass.append(miss)
                info(f"  unresolvable: {miss.miss_type}: {miss.name}")
                continue
            if pkg not in candidates:
                candidates[pkg] = miss
                info(f"  {miss.miss_type}: {miss.name} -> {pkg}")

        # Unresolved stall: misses exist but no candidate packages at all.
        if not candidates and misses:
            info(
                f"convergence: pass {pass_num} - all {len(misses)} "
                "miss(es) unresolvable; stalling"
            )
            result.stalled = True
            result.stall_reason = "unresolved"
            result.unresolved_misses = misses
            result.passes = pass_num
            break

        # Subtract packages already installed in the runner's environment.
        new_packages: list[str] = sorted(
            pkg for pkg in candidates
            if not runner.is_pkg_installed(pkg)
        )

        if not new_packages:
            info(
                f"convergence: pass {pass_num} - all resolved packages "
                "already installed; stalling"
            )
            result.stalled = True
            result.stall_reason = "no-new-packages"
            result.unresolved_misses = unresolved_this_pass
            result.passes = pass_num
            break

        # Large batch diagnostic.
        if len(new_packages) > _LARGE_BATCH_THRESHOLD:
            warning_msg = (
                f"pass {pass_num}: large batch ({len(new_packages)} "
                "packages) - possible mapping explosion or classifier issue"
            )
            info(f"convergence: WARNING - {warning_msg}")
            result.large_batch_warnings.append(warning_msg)

        # Record provenance (deduped: candidates dict ensures one per package).
        for pkg, miss in candidates.items():
            if pkg in new_packages:
                result.provenance.append(ProvenanceEntry(
                    package=pkg,
                    miss_type=miss.miss_type,
                    miss_name=miss.name,
                    required_by=miss.required_by,
                    pass_number=pass_num,
                ))

        result.unresolved_misses = unresolved_this_pass

        info(
            f"convergence: pass {pass_num} - installing {len(new_packages)} "
            f"package(s): {', '.join(new_packages)}"
        )
        rc = runner.apt_install(new_packages)
        if rc != 0:
            error(f"convergence: pass {pass_num} - apt install failed (fatal)")
            result.install_failed = True
            result.passes = pass_num
            result.log_file = str(last_log_file)
            _write_result(orthos, result)
            return result

        result.passes = pass_num

    else:
        # for-else: loop exhausted max passes without success or stall break.
        info(
            f"convergence: {_MAX_CONVERGENCE_PASSES} interrogation pass(es) "
            "exhausted without meson setup success"
        )
        result.stalled = True
        result.stall_reason = "no-new-packages"

    result.log_file = str(last_log_file)
    _write_result(orthos, result)
    return result
