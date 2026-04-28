"""Execution runner abstraction for Orthos convergence interrogation.

Defines the RunnerProtocol and two concrete implementations:

  HostRunner   — runs commands directly on the host. This is the pre-isolation
                 mode. Host package state affects outcomes. Invoked via
                 'deb smoke --host'.

  ChrootRunner — runs commands inside a prepared debootstrap chroot. This is
                 the authoritative isolated mode and the default for 'smoke'.

Privilege model:
  ChrootRunner does not contain any direct sudo subprocess calls. All
  chroot-mediated privileged actions are delegated to the privileged helper
  via deb.privileged.client. HostRunner is not affected — its apt_install
  call is host-level package management, outside the chroot lifecycle scope.

Contract:
  ChrootRunner assumes that ChrootEnv.setup_mounts() has already been called
  by the caller (the CLI) before any method is invoked. ChrootRunner does NOT
  manage mounts. run_convergence_loop() is filesystem-lifecycle agnostic — it
  receives a runner and calls its interface without knowing about chroot or mounts.

Path translation:
  Inside the chroot, the source repo is at /orthos/source and the build dir
  is at /orthos/build (bind-mounted by ChrootEnv.setup_mounts). Runners expose
  meson_source_path() and meson_build_path() so the convergence loop builds
  the correct meson setup command for each execution environment.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from deb.backends.build_backend_meson import _clean_env
from deb.privileged import client
from deb.privileged.launcher import PrivilegedHelperError


# Chroot-internal paths where source and build dirs are bind-mounted.
_CHROOT_SOURCE = "/orthos/source"
_CHROOT_BUILD = "/orthos/build"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RunnerProtocol(Protocol):
    """Narrow execution interface used by run_convergence_loop.

    All methods are deterministic and capture output for audit. The runner
    does not contain convergence logic — it only knows how to execute commands
    and manage packages in its own execution environment.
    """

    @property
    def mode(self) -> str:
        """Return "host" or "chroot". Written to convergence-result.json."""
        ...

    def run_command(
        self,
        cmd: list[str],
        log_file: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        """Run *cmd*, append output to *log_file*, return (success, output).

        Does not raise on non-zero exit. The log_file receives a command
        header line followed by combined stdout+stderr.
        """
        ...

    def apt_install(self, packages: list[str]) -> int:
        """Install *packages* in the runner's environment. Returns exit code."""
        ...

    def is_pkg_installed(self, package: str) -> bool:
        """Return True when *package* is installed in the runner's environment."""
        ...

    def meson_source_path(self, host_repo: Path) -> str:
        """Return the source repo path as seen inside this runner."""
        ...

    def meson_build_path(self, host_build_dir: Path) -> str:
        """Return the meson build dir path as seen inside this runner."""
        ...

    def pkg_query_exists(self, package: str) -> bool:
        """Return True when *package* is known to apt-cache in this environment.

        Used by miss_mapper as a fallback resolution step. In isolated mode
        this queries inside the chroot so host package metadata is not consulted.
        """
        ...

    def dpkg_search_path(self, pattern: str) -> str | None:
        """Return the package owning a path matching *pattern* via dpkg -S.

        Used by miss_mapper for header-miss resolution. Returns the normalised
        package name, or None if not found. In isolated mode this queries inside
        the chroot so only installed chroot packages are considered.
        """
        ...

    def apt_search_dev(self, meson_name: str) -> str | None:
        """Find a -dev package for *meson_name* in this runner's environment.

        Resolution order:
          1. pkg_query_exists(lib<name>-dev) — direct candidate check
          2. apt-cache search --names-only <name>.*-dev — broader search

        Returns a normalised package name, or None.
        Used by miss_mapper._dev_search and by the Pass 1 seed resolver
        in convergence.py, so both paths query the same environment.
        """
        ...

    def pkgconfig_file_search(self, name: str) -> str | None:
        """Return the package that owns *name*.pc in this runner's environment.

        Uses apt-file search against Contents metadata. In chroot mode this
        installs and updates apt-file inside the chroot on first use (guarded
        by a sentinel so cost is paid only once). In host mode returns None
        (the host path uses a different resolution strategy via miss_mapper).

        Returns a normalised package name, or None.
        """
        ...

    def pkg_query_version(self, package: str) -> str | None:
        """Return the installed version of *package* in this runner's environment.

        Uses dpkg-query -W. Returns None if the package is not installed.
        In chroot mode this queries inside the chroot; in host mode it queries
        the host. Used for target-version inspection during compatibility search.
        """
        ...

    def pkgconfig_modversion(self, module: str) -> str | None:
        """Return the pkg-config modversion for *module* in this environment.

        Returns None if the module is not available. In chroot mode this
        queries inside the chroot; in host mode it queries the host.
        Used for target-version inspection during compatibility search.
        """
        ...

    def pkg_candidate_version(self, package: str) -> str | None:
        """Return the apt candidate version of *package* in this environment.

        Uses apt-cache policy. Returns None if the package is unknown or has
        no candidate (e.g. not in any configured source).
        In chroot mode this queries the chroot's sources; in host mode it
        queries the host. Used to establish the distro source anchor before
        compatibility archaeology.
        """
        ...


# ---------------------------------------------------------------------------
# HostRunner
# ---------------------------------------------------------------------------


class HostRunner:
    """Run commands directly on the host system (pre-isolation mode).

    Host package state affects outcomes. The only environment sanitation
    applied is _clean_env() (strips active venv from PATH).

    Use 'deb smoke --host' to select this runner explicitly.
    """

    mode: str = "host"

    def run_command(
        self,
        cmd: list[str],
        log_file: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        actual_env = env if env is not None else _clean_env()
        result = subprocess.run(
            cmd,
            env=actual_env,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        output = result.stdout or ""
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {' '.join(cmd)}\n")
            fh.write(output)
            if not output.endswith("\n"):
                fh.write("\n")
        return result.returncode == 0, output

    def apt_install(self, packages: list[str]) -> int:
        if not packages:
            return 0
        result = subprocess.run(
            ["sudo", "apt", "install", "-y", *packages],
            check=False,
        )
        return result.returncode

    def is_pkg_installed(self, package: str) -> bool:
        result = subprocess.run(
            ["dpkg", "-s", package],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0

    def meson_source_path(self, host_repo: Path) -> str:
        return str(host_repo)

    def meson_build_path(self, host_build_dir: Path) -> str:
        return str(host_build_dir)

    def pkg_query_exists(self, package: str) -> bool:
        """Query apt-cache policy on the host to check package availability."""
        result = subprocess.run(
            ["apt-cache", "policy", package],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Candidate:"):
                candidate = stripped.split(":", 1)[1].strip()
                return candidate not in ("", "(none)")
        return False

    def dpkg_search_path(self, pattern: str) -> str | None:
        """Query dpkg -S on the host to find the package owning *pattern*."""
        try:
            result = subprocess.run(
                ["dpkg", "-S", f"*/{pattern}"],
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

    def apt_search_dev(self, meson_name: str) -> str | None:
        """Find a -dev package for *meson_name* on the host."""
        candidate = f"lib{meson_name}-dev"
        if self.pkg_query_exists(candidate):
            return candidate
        result = subprocess.run(
            ["apt-cache", "search", "--names-only", f"{meson_name}.*-dev"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            pkg = line.split()[0]
            if pkg:
                return pkg
        return None

    def pkgconfig_file_search(self, name: str) -> str | None:
        """Not implemented in host mode.

        Host-mode pkg-config resolution uses apt-file via _apt_file_search_host
        in miss_mapper (host-side). The chroot-specific pkgconfig-file-search
        operation is only meaningful for ChrootRunner.
        """
        return None

    def pkg_query_version(self, package: str) -> str | None:
        """Return the installed version of *package* on the host via dpkg-query."""
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Version}", package],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def pkgconfig_modversion(self, module: str) -> str | None:
        """Return the pkg-config modversion for *module* on the host."""
        try:
            result = subprocess.run(
                ["pkg-config", "--modversion", module],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    def pkg_candidate_version(self, package: str) -> str | None:
        """Return the apt candidate version of *package* on the host."""
        try:
            result = subprocess.run(
                ["apt-cache", "policy", package],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Candidate:"):
                candidate = stripped.split(":", 1)[1].strip()
                return candidate if candidate not in ("", "(none)") else None
        return None


# ---------------------------------------------------------------------------
# ChrootRunner
# ---------------------------------------------------------------------------


class ChrootRunner:
    """Run commands inside a prepared chroot (authoritative isolated mode).

    Requires that ChrootEnv.setup_mounts() has been called by the CLI before
    any method here is invoked. This class does not mount or unmount anything.

    All chroot-mediated privileged actions are delegated to the privileged
    helper via deb.privileged.client. No direct sudo subprocess calls
    exist in this class. Package state inside the chroot is fully isolated
    from the host.
    """

    mode: str = "chroot"

    def __init__(self, env: object) -> None:
        # env is a ChrootEnv; typed as object to avoid a circular import.
        # Callers guarantee it has a .root Path attribute.
        self._chroot_root: Path = env.root  # type: ignore[attr-defined]

    def run_command(
        self,
        cmd: list[str],
        log_file: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[bool, str]:
        # env is intentionally ignored for chroot runs: the chroot provides
        # a clean environment by construction. cwd is noted in the log header
        # but the chroot itself always starts from /.
        try:
            success, output = client.chroot_exec(self._chroot_root, cmd)
        except PrivilegedHelperError as exc:
            output = str(exc)
            success = False
        chroot_cmd_repr = f"chroot {self._chroot_root} {' '.join(cmd)}"
        cwd_note = f"  # cwd={cwd}" if cwd else ""
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n$ {chroot_cmd_repr}{cwd_note}\n")
            fh.write(output)
            if not output.endswith("\n"):
                fh.write("\n")
        return success, output

    def apt_install(self, packages: list[str]) -> int:
        if not packages:
            return 0
        try:
            return client.apt_install_in_chroot(self._chroot_root, packages)
        except PrivilegedHelperError:
            return 1

    def is_pkg_installed(self, package: str) -> bool:
        try:
            return client.pkg_query_installed(self._chroot_root, package)
        except PrivilegedHelperError:
            return False

    def meson_source_path(self, host_repo: Path) -> str:
        # Source repo is bind-mounted by ChrootEnv.setup_mounts() at this path.
        return _CHROOT_SOURCE

    def meson_build_path(self, host_build_dir: Path) -> str:
        # Build dir is bind-mounted by ChrootEnv.setup_mounts() at this path.
        return _CHROOT_BUILD

    def pkg_query_exists(self, package: str) -> bool:
        """Query apt-cache policy inside the chroot."""
        try:
            return client.pkg_query_exists(self._chroot_root, package)
        except PrivilegedHelperError:
            return False

    def dpkg_search_path(self, pattern: str) -> str | None:
        """Query dpkg -S inside the chroot to find the package owning *pattern*.

        Only finds packages already installed inside the chroot. In early
        convergence passes this will be sparse; the curated maps take priority.
        """
        try:
            return client.dpkg_search_path(self._chroot_root, pattern)
        except PrivilegedHelperError:
            return None

    def apt_search_dev(self, meson_name: str) -> str | None:
        """Find a -dev package for *meson_name* inside the chroot."""
        try:
            return client.apt_search_dev(self._chroot_root, meson_name)
        except PrivilegedHelperError:
            return None

    def pkgconfig_file_search(self, name: str) -> str | None:
        """Return the package that owns *name*.pc inside the chroot, or None.

        Delegates to the pkgconfig-file-search helper operation, which uses
        apt-file inside the chroot. First call installs apt-file and updates
        the Contents database (slow, once per chroot lifetime). Subsequent
        calls use the cached database.
        """
        try:
            return client.pkgconfig_file_search(self._chroot_root, name)
        except PrivilegedHelperError:
            return None

    def pkg_query_version(self, package: str) -> str | None:
        """Return the installed version of *package* inside the chroot, or None."""
        try:
            return client.pkg_query_version(self._chroot_root, package)
        except PrivilegedHelperError:
            return None

    def pkgconfig_modversion(self, module: str) -> str | None:
        """Return the pkg-config modversion for *module* inside the chroot, or None."""
        try:
            return client.pkgconfig_modversion(self._chroot_root, module)
        except PrivilegedHelperError:
            return None

    def pkg_candidate_version(self, package: str) -> str | None:
        """Return the apt candidate version of *package* inside the chroot, or None."""
        try:
            return client.pkg_candidate_version(self._chroot_root, package)
        except PrivilegedHelperError:
            return None
