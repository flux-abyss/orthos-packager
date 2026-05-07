#!/usr/bin/env python3
"""orthos-priv - privileged helper for Orthos chroot lifecycle operations.

Designed to be installed at a fixed path (e.g. /usr/local/bin/orthos-priv or
/usr/libexec/orthos-priv) and invoked by pkexec or sudo.

Invocation shape:
    orthos-priv <operation> --args '<json_object>'

Outputs a single JSON line to stdout:
    {"ok": true, "result": <value>}
    {"ok": false, "error": "<message>"}

Diagnostic log lines go to stderr. Exit code 0 on success, 1 on any failure.

Authorization model:
    Intended: pkexec with a polkit action gating allowed operations.
    Transitional: interactive sudo against this executable's fixed path.

    The correct sudoers entry shape (if sudo bridge is used during development):
        <user> ALL=(root) NOPASSWD: /usr/local/bin/orthos-priv
    Target the fixed executable path - never a broad python3 -m ... rule.

Allowlisted operations:
    create-chroot                  debootstrap + post-setup
    setup-mounts                   bind-mount proc/sys/dev/devpts/source/build/logs
    teardown-mounts                unmount listed paths in reverse order
    apt-install-in-chroot          chroot apt-get install
    chroot-exec                    run an allowlisted command inside the chroot
    pkg-query-installed            chroot dpkg -s
    pkg-query-exists               chroot apt-cache policy
    pkg-candidate-version          chroot apt-cache policy candidate version
    dpkg-search-path               chroot dpkg -S
    apt-search-dev                 chroot apt-cache search (dev package lookup)
    pkgconfig-file-search          chroot apt-file search for <name>.pc - returns owning package
    apt-file-search-absolute-path  chroot apt-file search for absolute file path
    pkg-query-version              chroot dpkg-query -W version query
    pkgconfig-modversion           chroot pkg-config --modversion query
    destroy-chroot                 rm -rf <root>
    reset-chroot                   teardown mounts then destroy
    destroy-convergence-work       clean root-owned convergence files
    destroy-build-src              clean root-owned leftover build files

Path validation:
    All chroot root paths must be absolute and contain /.orthos/ as a path
    component. destroy-chroot and reset-chroot additionally require the final
    path component to match the safe <target-name>-<arch> pattern (under
    .orthos/chroots/) to prevent accidental broad deletes.
    The helper fails closed on any validation failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from deb.privileged.protocol import _ok, _fail, _log
from deb.privileged.run import _run
from deb.privileged.validate import (
    _validate_chroot_root,
    _validate_destroy_root,
    _validate_convergence_work_dir,
    _validate_build_src_dir,
    _validate_bind_dst,
)
from deb.privileged.mounts import _is_mounted, _mount_bind, _mount_special


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEBOOTSTRAP = "/usr/sbin/debootstrap"
_DEFAULT_SUITE = "trixie"
_DEBIAN_MIRROR = "http://deb.debian.org/debian"



_BASE_PACKAGES: list[str] = [
    "build-essential",
    "meson",
    "ninja-build",
    "pkg-config",
    "python3",
]

# Commands whose first element is permitted inside chroot-exec.
# Extend this list only for concrete, known needs.
_CHROOT_EXEC_ALLOWED_COMMANDS: frozenset[str] = frozenset([
    "meson",
    "ninja",
    "pkg-config",
    "dpkg",
    "apt-get",
    "apt-cache",
    "python3",
    "bash",
    "dpkg-buildpackage",
])


def _internal_pkg_query_exists(root: Path, package: str) -> bool:
    """Query apt-cache policy inside *root*. Returns True if a candidate exists."""
    result = subprocess.run(
        ["chroot", str(root), "apt-cache", "policy", package],
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


def _internal_pkg_candidate_version(root: Path, package: str) -> str | None:
    """Return the apt candidate version string for *package* inside *root*, or None."""
    result = subprocess.run(
        ["chroot", str(root), "apt-cache", "policy", package],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            candidate = stripped.split(":", 1)[1].strip()
            return candidate if candidate not in ("", "(none)") else None
    return None


# ---------------------------------------------------------------------------
# Operation implementations
# ---------------------------------------------------------------------------

def _ensure_chroot_root_owner(
    root: Path,
    log_fh: Any | None = None,
    create_missing: bool = True,
) -> None:
    if create_missing:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.exists():
        raise RuntimeError(f"chroot root does not exist: {root}")

    st = root.stat()
    mode = st.st_mode & 0o777
    if st.st_uid == 0 and st.st_gid == 0 and mode == 0o755:
        return
    
    _log(f"orthos-priv: repairing chroot root ownership for {root}")
    if log_fh and hasattr(log_fh, "write"):
        log_fh.write(f"\n# repairing root ownership/permissions for {root}\n")
        log_fh.flush()
    
    os.chown(str(root), 0, 0)
    root.chmod(0o755)



def _op_create_chroot(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    suite = str(args.get("suite", _DEFAULT_SUITE))
    mirror = str(args.get("mirror", _DEBIAN_MIRROR))
    repo_set = args.get("repo_set")
    if repo_set is not None:
        repo_set = str(repo_set)
    log_file_path: str | None = args.get("log_file")
    log_fh = open(log_file_path, "w", encoding="utf-8") if log_file_path else None  # noqa: WPS515

    try:
        _ensure_chroot_root_owner(root, log_fh, create_missing=True)
        _log(f"orthos-priv: create-chroot start: root={root} suite={suite}")

        # Step 1: debootstrap
        _run(
            [
                _DEBOOTSTRAP,
                "--variant=minbase",
                "--include=ca-certificates,apt-transport-https",
                suite,
                str(root),
                mirror,
            ],
            f"debootstrap {suite} from {mirror}",
            log_fh,
        )

        # Step 2: DNS
        _log("orthos-priv: copying /etc/resolv.conf")
        if log_fh:
            log_fh.write("\n# DNS: copy resolv.conf\n")
            log_fh.flush()
        subprocess.run(
            ["cp", "/etc/resolv.conf", str(root / "etc" / "resolv.conf")],
            check=True,
        )

        from deb.target_repos import get_target_repo_profile
        profile = get_target_repo_profile(repo_set)

        # Step 3 and 4: Target repo profile apt source and keyring
        if profile.apt_source_line:
            profile_list = root / "etc" / "apt" / "sources.list.d" / f"{profile.name}.list"
            _log(f"orthos-priv: writing {profile.name} source -> {profile_list}")
            if log_fh:
                log_fh.write(f"\n# {profile.name} source injection\n{profile.apt_source_line}\n")
                log_fh.flush()
            profile_list.parent.mkdir(parents=True, exist_ok=True)
            profile_list.write_text(profile.apt_source_line + "\n", encoding="utf-8")

        if profile.keyring_host_path and profile.keyring_chroot_path:
            if profile.keyring_host_path.exists():
                _log(f"orthos-priv: copying {profile.name} keyring")
                if log_fh:
                    log_fh.write(f"\n# {profile.name} keyring: {profile.keyring_host_path}\n")
                    log_fh.flush()
                # Remove leading slash to make it a relative path before appending to root
                rel_chroot_path = str(profile.keyring_chroot_path).lstrip("/")
                chroot_keyring_file = root / rel_chroot_path
                chroot_keyring_file.parent.mkdir(parents=True, exist_ok=True)
                subprocess.run(
                    [
                        "cp",
                        str(profile.keyring_host_path),
                        str(chroot_keyring_file),
                    ],
                    check=True,
                )
            else:
                raise RuntimeError(
                    f"{profile.name} keyring not found at {profile.keyring_host_path}. "
                    "Ensure the keyring file exists."
                )

        # Step 5: apt-get update
        _run(
            ["chroot", str(root), "apt-get", "update"],
            "apt-get update",
            log_fh,
        )

        # Step 6: base packages
        _run(
            [
                "chroot", str(root),
                "apt-get", "install", "-y", "--no-install-recommends",
                *_BASE_PACKAGES,
            ],
            f"install base packages: {', '.join(_BASE_PACKAGES)}",
            log_fh,
        )

        if log_fh:
            log_fh.write(f"\n# chroot ready: {root}\n")
        _log(f"orthos-priv: create-chroot done: {root}")

    finally:
        if log_fh:
            log_fh.flush()
            log_fh.close()

    _ok()


def _op_setup_mounts(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    source_repo = Path(args["source_repo"])
    build_dir = Path(args["build_dir"])
    logs_dir = Path(args["logs_dir"])

    _ensure_chroot_root_owner(root, create_missing=False)

    _log(f"orthos-priv: setup-mounts start: root={root}")

    # Tracks mount points created during this invocation for rollback on failure.
    mounted: list[str] = []

    def _bind(src: str | Path, dst: Path, read_only: bool = False) -> None:
        _validate_bind_dst(root, dst)
        # Preflight: if the destination is already mounted, fail clearly.
        if _is_mounted(dst):
            raise RuntimeError(
                f"setup-mounts: destination already mounted, refusing to overlay: {dst}"
            )
        _mount_bind(src, dst, read_only=read_only)
        mounted.append(str(dst))

    def _special(fstype: str, dst: Path) -> None:
        _validate_bind_dst(root, dst)
        if _is_mounted(dst):
            raise RuntimeError(
                f"setup-mounts: destination already mounted, refusing to overlay: {dst}"
            )
        _mount_special(fstype, dst)
        mounted.append(str(dst))

    def _rollback() -> None:
        """Unmount, in reverse order, every mount created so far this call."""
        _log(f"orthos-priv: setup-mounts rollback: cleaning {len(mounted)} mount(s)")
        for path_str in reversed(mounted):
            result = subprocess.run(
                ["umount", path_str],
                check=False,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                _log(f"orthos-priv: rollback: umount {path_str}")
            else:
                err = (result.stderr or "").strip()
                _log(f"orthos-priv: rollback: WARNING - umount {path_str} failed: {err}")

    try:
        _bind("/proc", root / "proc")
        _bind("/sys", root / "sys")
        _bind("/dev", root / "dev")
        _special("devpts", root / "dev" / "pts")
        _bind(source_repo, root / "orthos" / "source", read_only=True)
        _bind(build_dir, root / "orthos" / "build")
        _bind(logs_dir, root / "orthos" / "logs")
        if "build_src" in args:
            _bind(Path(args["build_src"]), root / "orthos" / "build-src")
    except (RuntimeError, ValueError):
        _rollback()
        raise

    _log(f"orthos-priv: setup-mounts done: {len(mounted)} mounts")
    _ok(mounted)


def _op_teardown_mounts(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    mounts: list[str] = list(args.get("mounts", []))

    _log(f"orthos-priv: teardown-mounts start: {len(mounts)} path(s)")
    failures: list[str] = []

    for mount_path_str in reversed(mounts):
        mount_path = Path(mount_path_str)
        # Safety: mount path must be inside the validated chroot root.
        try:
            _validate_bind_dst(root, mount_path)
        except ValueError as exc:
            _log(f"orthos-priv: WARNING - skipping unsafe mount path: {exc}")
            failures.append(mount_path_str)
            continue

        result = subprocess.run(
            ["umount", str(mount_path)],
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            _log(f"orthos-priv: umount {mount_path}")
        else:
            err = (result.stderr or "").strip()
            _log(f"orthos-priv: WARNING - umount {mount_path} failed: {err}")
            failures.append(mount_path_str)

    _log("orthos-priv: teardown-mounts done")
    _ok({"failures": failures})


def _op_apt_install_in_chroot(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    packages: list[str] = list(args["packages"])

    if not packages:
        _ok(0)
        return

    _log(f"orthos-priv: apt-install-in-chroot: root={root} packages={packages}")
    result = subprocess.run(
        [
            "chroot", str(root),
            "apt-get", "install", "-y", "--no-install-recommends",
            *packages,
        ],
        stdout=sys.stderr,
        stderr=sys.stderr,
        check=False,
        text=True,
    )
    _log(f"orthos-priv: apt-install-in-chroot done: rc={result.returncode}")
    _ok(result.returncode)


def _op_chroot_exec(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    cmd: list[str] = list(args["cmd"])

    if not cmd:
        _fail("chroot-exec: empty command")
        return

    executable = Path(cmd[0]).name  # strip any path prefix, check basename
    if executable not in _CHROOT_EXEC_ALLOWED_COMMANDS:
        _fail(
            f"chroot-exec: command not in allowlist: {cmd[0]!r}. "
            f"Allowed: {sorted(_CHROOT_EXEC_ALLOWED_COMMANDS)}"
        )
        return

    full_cmd = ["chroot", str(root), *cmd]
    _log(f"orthos-priv: chroot-exec: {' '.join(full_cmd)}")
    result = subprocess.run(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = result.stdout or ""
    _log(f"orthos-priv: chroot-exec done: rc={result.returncode}")
    _ok({"returncode": result.returncode, "output": output})


def _op_pkg_query_installed(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    package = str(args["package"])

    result = subprocess.run(
        ["chroot", str(root), "dpkg", "-s", package],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    _ok(result.returncode == 0)


def _op_pkg_query_exists(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    package = str(args["package"])

    exists = _internal_pkg_query_exists(root, package)
    _ok(exists)


def _op_pkg_candidate_version(args: dict) -> None:
    """Return the apt candidate version for *package* inside *root*, or None."""
    root = _validate_chroot_root(Path(args["root"]))
    package = str(args["package"])

    version = _internal_pkg_candidate_version(root, package)
    _ok(version)


def _op_dpkg_search_path(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    pattern = str(args["pattern"])

    try:
        result = subprocess.run(
            ["chroot", str(root), "dpkg", "-S", f"*/{pattern}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        _ok(None)
        return

    if result.returncode != 0 or not result.stdout.strip():
        _ok(None)
        return

    line = result.stdout.strip().splitlines()[0]
    if ":" in line:
        pkg = line.split(":")[0].strip()
        _ok(pkg.lower() if pkg else None)
    else:
        _ok(None)


def _op_apt_search_dev(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    meson_name = str(args["meson_name"])

    candidate = f"lib{meson_name}-dev"
    if _internal_pkg_query_exists(root, candidate):
        _ok(candidate)
        return

    result = subprocess.run(
        [
            "chroot", str(root),
            "apt-cache", "search", "--names-only", f"{meson_name}.*-dev",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        pkg = line.split()[0] if line.split() else ""
        if pkg:
            _ok(pkg)
            return

    _ok(None)


# ---------------------------------------------------------------------------
# pkgconfig-file-search - find the package providing <name>.pc via apt-file
# ---------------------------------------------------------------------------

# Sentinel written inside the chroot after the first apt-file db update so we
# don't pay the update cost on every lookup during a single convergence run.
_APT_FILE_DB_SENTINEL = ".orthos-apt-file-updated"


def _ensure_apt_file(root: Path) -> None:
    """Install apt-file and update its database inside *root* if not done yet.

    Idempotent: guarded by a sentinel file so the heavy update only runs once
    per chroot. Raises RuntimeError if installation or update fails.
    """
    sentinel = root / "tmp" / _APT_FILE_DB_SENTINEL
    if sentinel.exists():
        return

    # Install apt-file if not present.
    check = subprocess.run(
        ["chroot", str(root), "dpkg", "-s", "apt-file"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if check.returncode != 0:
        _log("orthos-priv: pkgconfig-file-search: installing apt-file")
        install = subprocess.run(
            [
                "chroot", str(root),
                "apt-get", "install", "-y", "--no-install-recommends", "apt-file",
            ],
            stdout=sys.stderr,
            stderr=sys.stderr,
            check=False,
        )
        if install.returncode != 0:
            raise RuntimeError("pkgconfig-file-search: failed to install apt-file")

    # Update the apt-file database (fetches Contents files from apt sources).
    _log("orthos-priv: pkgconfig-file-search: updating apt-file database")
    update = subprocess.run(
        ["chroot", str(root), "apt-file", "update"],
        stdout=sys.stderr,
        stderr=sys.stderr,
        check=False,
    )
    if update.returncode != 0:
        raise RuntimeError("pkgconfig-file-search: apt-file update failed")

    # Write sentinel so subsequent calls skip the update.
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()


def _op_pkgconfig_file_search(args: dict) -> None:
    """Find the Debian package that owns <pkgconfig_name>.pc inside *root*.

    Strategy:
      1. Ensure apt-file is installed and its database is current (once per chroot).
      2. Run: apt-file search --regexp '(usr/lib/.*/pkgconfig|usr/share/pkgconfig)/<name>\.pc$'
         This queries Contents metadata and restricts matches to the two canonical
         pkg-config directory trees.  A substring search on bare '<name>.pc' is NOT
         used because it would match Go import paths, documentation, examples, and
         any other path component that happens to contain the filename.
      3. Among all candidates:
         a. Prefer *-dev packages (deterministic: alphabetical first among -dev).
         b. Fall back to the alphabetical-first non-dev package.
      4. Return None if no package is found.

    Returns a single package name string, or null.
    """
    root = _validate_chroot_root(Path(args["root"]))
    name = str(args["name"]).strip().lower()
    # Anchor to canonical pkg-config install directories only.
    # This rejects matches in Go source trees, documentation paths, examples,
    # and any other location that is not a real pkg-config provider.
    pc_pattern = (
        f"(usr/lib/.*/pkgconfig|usr/share/pkgconfig)/{re.escape(name)}\.pc$"
    )

    _log(f"orthos-priv: pkgconfig-file-search: pattern={pc_pattern!r}")

    try:
        _ensure_apt_file(root)
    except RuntimeError as exc:
        _log(f"orthos-priv: pkgconfig-file-search: {exc} - returning None")
        _ok(None)
        return

    result = subprocess.run(
        [
            "chroot", str(root),
            "apt-file", "search", "--regexp", "--package-only", pc_pattern,
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    candidates: list[str] = [
        line.strip().lower()
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    if not candidates:
        _log(f"orthos-priv: pkgconfig-file-search: no pkgconfig provider for {name!r}")
        _ok(None)
        return

    # Prefer -dev packages; among equals choose alphabetical first for determinism.
    dev_candidates = sorted(p for p in candidates if p.endswith("-dev"))
    if dev_candidates:
        chosen = dev_candidates[0]
    else:
        chosen = sorted(candidates)[0]

    _log(f"orthos-priv: pkgconfig-file-search: {name!r} -> {chosen}")
    _ok(chosen)


def _op_apt_file_search_absolute_path(args: dict) -> None:
    """Find the Debian package that owns an absolute path inside *root*."""
    root = _validate_chroot_root(Path(args["root"]))
    path = str(args["path"]).strip()
    
    # Strip leading slash to match apt-file contents format
    search_path = path.lstrip("/")
    
    _log(f"orthos-priv: apt-file-search-absolute-path: path={search_path!r}")
    
    try:
        _ensure_apt_file(root)
    except RuntimeError as exc:
        _log(f"orthos-priv: apt-file-search-absolute-path: {exc} - returning None")
        _ok(None)
        return

    # Exact match for the path
    pattern = f"^{re.escape(search_path)}$"
    
    result = subprocess.run(
        [
            "chroot", str(root),
            "apt-file", "search", "--regexp", "--package-only", pattern,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    
    candidates: list[str] = [
        line.strip().lower()
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    
    if not candidates:
        _log(f"orthos-priv: apt-file-search-absolute-path: no provider for {path!r}")
        _ok(None)
        return
        
    chosen = sorted(candidates)[0]
    _log(f"orthos-priv: apt-file-search-absolute-path: resolved {path!r} -> {chosen}")
    _ok(chosen)


def _op_pkg_query_version(args: dict) -> None:
    """Return the installed version of *package* inside *root* via dpkg-query.

    Returns the version string, or None if the package is not installed.
    """
    root = _validate_chroot_root(Path(args["root"]))
    package = str(args["package"])

    result = subprocess.run(
        [
            "chroot", str(root),
            "dpkg-query", "-W", "-f=${Version}", package,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        _ok(None)
        return
    _ok(result.stdout.strip())


def _op_pkgconfig_modversion(args: dict) -> None:
    """Return the pkg-config modversion for *module* inside *root*.

    Returns the version string, or None if the module is not found.
    """
    root = _validate_chroot_root(Path(args["root"]))
    module = str(args["module"])

    result = subprocess.run(
        [
            "chroot", str(root),
            "pkg-config", "--modversion", module,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        _ok(None)
        return
    _ok(result.stdout.strip())


def _op_destroy_chroot(args: dict) -> None:
    root = _validate_destroy_root(Path(args["root"]))

    _log(f"orthos-priv: destroy-chroot: {root}")
    if not root.exists():
        _log(f"orthos-priv: destroy-chroot: path does not exist, nothing to do")
        _ok()
        return

    subprocess.run(["rm", "-rf", str(root)], check=True)
    _log(f"orthos-priv: destroy-chroot done: {root}")
    _ok()


def _op_reset_chroot(args: dict) -> None:
    """Teardown any known mounts under root, then destroy the chroot tree."""
    root = _validate_destroy_root(Path(args["root"]))

    _log(f"orthos-priv: reset-chroot start: {root}")

    # Unmount any filesystems still mounted under root (best-effort, reversed).
    # We read /proc/mounts to find active mounts rather than trusting a stored list,
    # since the caller may not have a reliable list (e.g. on manual reset).
    active_under_root: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    mount_point = parts[1]
                    try:
                        Path(mount_point).relative_to(root)
                        active_under_root.append(mount_point)
                    except ValueError:
                        pass
    except OSError:
        pass  # /proc/mounts unavailable; proceed to removal

    # Sort longest first so nested mounts are unmounted before parents.
    active_under_root.sort(key=len, reverse=True)
    for mount_point in active_under_root:
        result = subprocess.run(
            ["umount", mount_point],
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0:
            _log(f"orthos-priv: reset-chroot: umount {mount_point}")
        else:
            err = (result.stderr or "").strip()
            _log(f"orthos-priv: reset-chroot: WARNING - umount {mount_point}: {err}")

    # Remove the chroot tree.
    if root.exists():
        subprocess.run(["rm", "-rf", str(root)], check=True)
        _log(f"orthos-priv: reset-chroot: removed {root}")
    else:
        _log(f"orthos-priv: reset-chroot: {root} did not exist")

    _log("orthos-priv: reset-chroot done")
    _ok()


def _op_destroy_convergence_work(args: dict) -> None:
    """Remove a per-project convergence work directory under .orthos/chroot-work/.

    Used by reset-chroot to clean root-owned Meson build output left by
    chroot convergence runs.  Path is strictly validated to be at least
    two levels below .orthos/chroot-work/.
    """
    path = _validate_convergence_work_dir(Path(args["path"]))

    _log(f"orthos-priv: destroy-convergence-work: {path}")
    if not path.exists():
        _log(f"orthos-priv: destroy-convergence-work: path does not exist, nothing to do")
        _ok()
        return

    subprocess.run(["rm", "-rf", str(path)], check=True)
    _log(f"orthos-priv: destroy-convergence-work done: {path}")
    _ok()


def _op_destroy_build_src(args: dict) -> None:
    """Remove the build-src directory under .orthos/<repo>/build-src/.

    Used to clean root-owned files left when dpkg-buildpackage ran inside the
    chroot with build-src bind-mounted.  Path is strictly validated to end with
    the 'build-src' component and be under a .orthos/ workspace.
    """
    path = _validate_build_src_dir(Path(args["path"]))

    _log(f"orthos-priv: destroy-build-src: {path}")
    if not path.exists():
        _log(f"orthos-priv: destroy-build-src: path does not exist, nothing to do")
        _ok()
        return

    subprocess.run(["rm", "-rf", str(path)], check=True)
    _log(f"orthos-priv: destroy-build-src done: {path}")
    _ok()


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_OPERATIONS: dict = {
    "create-chroot":              _op_create_chroot,
    "setup-mounts":               _op_setup_mounts,
    "teardown-mounts":            _op_teardown_mounts,
    "apt-install-in-chroot":      _op_apt_install_in_chroot,
    "chroot-exec":                _op_chroot_exec,
    "pkg-query-installed":        _op_pkg_query_installed,
    "pkg-query-exists":           _op_pkg_query_exists,
    "pkg-candidate-version":      _op_pkg_candidate_version,
    "pkg-query-version":          _op_pkg_query_version,
    "dpkg-search-path":           _op_dpkg_search_path,
    "apt-search-dev":             _op_apt_search_dev,
    "pkgconfig-file-search":      _op_pkgconfig_file_search,
    "pkgconfig-modversion":       _op_pkgconfig_modversion,
    "apt-file-search-absolute-path": _op_apt_file_search_absolute_path,
    "destroy-chroot":             _op_destroy_chroot,
    "reset-chroot":               _op_reset_chroot,
    "destroy-convergence-work":   _op_destroy_convergence_work,
    "destroy-build-src":          _op_destroy_build_src,
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Main entry point for the orthos-priv helper executable."""
    parser = argparse.ArgumentParser(
        prog="orthos-priv",
        description="Privileged helper for Orthos chroot lifecycle operations.",
    )
    parser.add_argument(
        "operation",
        metavar="OPERATION",
        choices=sorted(_OPERATIONS),
        help=f"Operation to perform. One of: {', '.join(sorted(_OPERATIONS))}",
    )
    parser.add_argument(
        "--args",
        metavar="JSON",
        default="{}",
        help="Operation arguments as a JSON object string.",
    )
    parsed = parser.parse_args()

    try:
        op_args = json.loads(parsed.args)
    except json.JSONDecodeError as exc:
        _fail(f"invalid --args JSON: {exc}")
        sys.exit(1)

    handler = _OPERATIONS.get(parsed.operation)
    if handler is None:
        # argparse choices= should prevent this, but be defensive.
        _fail(f"unknown operation: {parsed.operation!r}")
        sys.exit(1)

    try:
        handler(op_args)
    except (ValueError, RuntimeError) as exc:
        _fail(str(exc))
        sys.exit(1)
    except Exception as exc:  # pylint: disable=broad-except
        _fail(f"unexpected error in {parsed.operation!r}: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
