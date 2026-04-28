"""Unprivileged client API for Orthos chroot lifecycle operations.

This module provides the narrow interface used by the unprivileged core
(chroot_env.py, runner.py, cli/main.py) to request privileged chroot operations.

Each function validates Python-side inputs, serializes arguments, and calls
launcher.invoke(). The actual privileged execution happens in helper.py via
the launcher's chosen auth backend (sudo bridge or pkexec/polkit).

The caller sees PrivilegedHelperError on any failure. ChrootEnv converts
these to ChrootEnvError where appropriate.

Public API:
    create_chroot(root, suite, mirror, log_file)
    setup_mounts(root, source_repo, build_dir, logs_dir) -> list[Path]
    teardown_mounts(root, mounts)
    apt_install_in_chroot(root, packages) -> int
    chroot_exec(root, cmd) -> tuple[bool, str]
    pkg_query_installed(root, package) -> bool
    pkg_query_exists(root, package) -> bool
    pkg_query_version(root, package) -> str | None
    dpkg_search_path(root, pattern) -> str | None
    apt_search_dev(root, meson_name) -> str | None
    pkgconfig_file_search(root, name) -> str | None
    pkgconfig_modversion(root, module) -> str | None
    destroy_chroot(root)
    reset_chroot(root)
"""

from __future__ import annotations

from pathlib import Path

from deb.privileged.launcher import PrivilegedHelperError, invoke

__all__ = [
    "PrivilegedHelperError",
    "create_chroot",
    "setup_mounts",
    "teardown_mounts",
    "apt_install_in_chroot",
    "chroot_exec",
    "pkg_query_installed",
    "pkg_query_exists",
    "pkg_candidate_version",
    "pkg_query_version",
    "dpkg_search_path",
    "apt_search_dev",
    "pkgconfig_file_search",
    "pkgconfig_modversion",
    "destroy_chroot",
    "reset_chroot",
]


def create_chroot(
    root: Path,
    suite: str = "trixie",
    mirror: str = "http://deb.debian.org/debian",
    log_file: Path | None = None,
) -> None:
    """Create a debootstrap chroot at *root* and run post-setup.

    Steps (performed by the helper):
      1. debootstrap --variant=minbase
      2. Copy /etc/resolv.conf
      3. Write Bodhi apt source
      4. Copy Bodhi keyring (hard requirement)
      5. apt-get update
      6. apt-get install base packages

    Raises PrivilegedHelperError on any step failure.
    """
    args: dict = {
        "root": str(root),
        "suite": suite,
        "mirror": mirror,
    }
    if log_file is not None:
        args["log_file"] = str(log_file)
    invoke("create-chroot", args)


def setup_mounts(
    root: Path,
    source_repo: Path,
    build_dir: Path,
    logs_dir: Path,
) -> list[Path]:
    """Bind-mount proc/sys/dev/devpts/source/build/logs into *root*.

    Returns the list of mounted paths so the caller can store them for
    teardown_mounts(). Raises PrivilegedHelperError on any mount failure.
    """
    result = invoke("setup-mounts", {
        "root": str(root),
        "source_repo": str(source_repo),
        "build_dir": str(build_dir),
        "logs_dir": str(logs_dir),
    })
    mounted_strs: list[str] = result.get("result") or []
    return [Path(p) for p in mounted_strs]


def teardown_mounts(root: Path, mounts: list[Path]) -> None:
    """Unmount *mounts* (in reverse order) under *root*.

    Does not raise on individual umount failures; failures are logged by the
    helper. Raises PrivilegedHelperError only on hard launch failures.
    """
    invoke("teardown-mounts", {
        "root": str(root),
        "mounts": [str(m) for m in mounts],
    })


def apt_install_in_chroot(root: Path, packages: list[str]) -> int:
    """Install *packages* inside *root* via apt-get. Returns the exit code."""
    if not packages:
        return 0
    result = invoke("apt-install-in-chroot", {
        "root": str(root),
        "packages": packages,
    })
    return int(result.get("result", 0))


def chroot_exec(root: Path, cmd: list[str]) -> tuple[bool, str]:
    """Run *cmd* inside *root*. Returns (success, combined_output).

    *cmd* must start with an allowlisted executable (meson, ninja, pkg-config,
    dpkg, apt-get, apt-cache, python3). The helper rejects anything else.
    """
    result = invoke("chroot-exec", {
        "root": str(root),
        "cmd": cmd,
    })
    payload: dict = result.get("result") or {}
    rc: int = int(payload.get("returncode", 1))
    output: str = str(payload.get("output", ""))
    return rc == 0, output


def pkg_query_installed(root: Path, package: str) -> bool:
    """Return True when *package* is installed inside *root* (dpkg -s)."""
    result = invoke("pkg-query-installed", {
        "root": str(root),
        "package": package,
    })
    return bool(result.get("result", False))


def pkg_query_exists(root: Path, package: str) -> bool:
    """Return True when *package* has an apt candidate inside *root*."""
    result = invoke("pkg-query-exists", {
        "root": str(root),
        "package": package,
    })
    return bool(result.get("result", False))


def pkg_query_version(root: Path, package: str) -> str | None:
    """Return the installed version of *package* inside *root*, or None."""
    result = invoke("pkg-query-version", {
        "root": str(root),
        "package": package,
    })
    val = result.get("result")
    return str(val) if val else None


def pkg_candidate_version(root: Path, package: str) -> str | None:
    """Return the apt candidate version of *package* inside *root*, or None."""
    result = invoke("pkg-candidate-version", {
        "root": str(root),
        "package": package,
    })
    val = result.get("result")
    return str(val) if val else None


def dpkg_search_path(root: Path, pattern: str) -> str | None:
    """Return the package owning *pattern* via dpkg -S inside *root*, or None."""
    result = invoke("dpkg-search-path", {
        "root": str(root),
        "pattern": pattern,
    })
    val = result.get("result")
    return str(val) if val else None


def apt_search_dev(root: Path, meson_name: str) -> str | None:
    """Find a -dev package for *meson_name* inside *root*, or None."""
    result = invoke("apt-search-dev", {
        "root": str(root),
        "meson_name": meson_name,
    })
    val = result.get("result")
    return str(val) if val else None


def pkgconfig_file_search(root: Path, name: str) -> str | None:
    """Return the package that owns *name*.pc inside *root*, or None.

    Uses apt-file search inside the chroot against the installed Contents
    metadata. apt-file is installed and its database updated on first use
    (idempotent, guarded by a sentinel). Prefers *-dev packages among
    multiple candidates.

    This is the authoritative .pc-file resolver: it finds the actual package
    that ships the pkg-config module, with no hardcoded maps.
    """
    result = invoke("pkgconfig-file-search", {
        "root": str(root),
        "name": name,
    })
    val = result.get("result")
    return str(val) if val else None


def pkgconfig_modversion(root: Path, module: str) -> str | None:
    """Return the pkg-config modversion for *module* inside *root*, or None."""
    result = invoke("pkgconfig-modversion", {
        "root": str(root),
        "module": module,
    })
    val = result.get("result")
    return str(val) if val else None


def destroy_chroot(root: Path) -> None:
    """Remove the chroot tree at *root* (rm -rf, path-validated).

    *root* must end with a 'chroot' path component. Raises PrivilegedHelperError
    if the path fails validation or removal fails.
    """
    invoke("destroy-chroot", {"root": str(root)})


def reset_chroot(root: Path) -> None:
    """Teardown active mounts under *root* and remove the chroot tree.

    Reads /proc/mounts to find mounts (does not rely on a stored list).
    Suitable for the reset-chroot CLI command and for --refresh-chroot.
    *root* must end with a 'chroot' path component.
    """
    invoke("reset-chroot", {"root": str(root)})
