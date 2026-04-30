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
    create-chroot         debootstrap + post-setup
    setup-mounts          bind-mount proc/sys/dev/devpts/source/build/logs
    teardown-mounts       unmount listed paths in reverse order
    apt-install-in-chroot chroot apt-get install
    chroot-exec           run an allowlisted command inside the chroot
    pkg-query-installed   chroot dpkg -s
    pkg-query-exists      chroot apt-cache policy
    dpkg-search-path      chroot dpkg -S
    apt-search-dev        chroot apt-cache search (dev package lookup)
    pkgconfig-file-search chroot apt-file search for <name>.pc - returns owning package
    destroy-chroot        rm -rf <root>
    reset-chroot          teardown mounts then destroy

Path validation:
    All chroot root paths must be absolute and contain /.orthos/ as a path
    component. destroy-chroot and reset-chroot additionally require the path
    to end with a /chroot segment to prevent accidental broad deletes.
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEBOOTSTRAP = "/usr/sbin/debootstrap"
_DEFAULT_SUITE = "trixie"
_DEBIAN_MIRROR = "http://deb.debian.org/debian"

_BODHI_SOURCE_LINE = (
    "deb [signed-by=/usr/share/keyrings/bodhi-archive-keyring.gpg]"
    " http://packages.bodhilinux.com/bodhi/ lila b8debbie"
)
_BODHI_KEYRING_HOST = Path("/usr/share/keyrings/bodhi-archive-keyring.gpg")

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
])


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def _validate_chroot_root(root: Path) -> Path:
    """Return the resolved chroot root if it passes all validation checks.

    Raises ValueError with a descriptive message on any failure.
    Checks:
      - Must be absolute after resolution.
      - Must contain /.orthos/ as a path component (prevents arbitrary targets).
      - No path traversal after resolution.
    """
    try:
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve chroot root path: {exc}") from exc

    if not resolved.is_absolute():
        raise ValueError(f"chroot root must be absolute: {resolved}")

    parts = resolved.parts
    if ".orthos" not in parts:
        raise ValueError(
            f"chroot root must be under a .orthos/ workspace directory: {resolved}"
        )

    return resolved


def _validate_destroy_root(root: Path) -> Path:
    """Like _validate_chroot_root, plus additional safety checks for destroy/reset.

    The shared chroot path is:
        .orthos/chroots/<suite>-<arch>/

    Checks (in addition to _validate_chroot_root):
      - Must be under a .orthos/chroots/ directory component.
      - The final path component must match the safe suite-arch pattern:
            ^[a-z0-9.+-]+-[a-z0-9_]+$
        This accepts names like "trixie-amd64", "bookworm-arm64", "sid-amd64".
      - The target must not be the .orthos/ or .orthos/chroots/ directory itself.

    These constraints prevent accidentally targeting a broad workspace directory.
    """
    import re as _re  # local to avoid adding a module-level import for one guard
    resolved = _validate_chroot_root(root)

    parts = resolved.parts
    # Must have .orthos/chroots/ somewhere in the ancestry.
    try:
        orthos_idx = parts.index(".orthos")
    except ValueError:
        # Already caught by _validate_chroot_root, but be defensive.
        raise ValueError(
            f"destroy/reset target must be under .orthos/: {resolved}"
        )
    if orthos_idx + 1 >= len(parts) or parts[orthos_idx + 1] != "chroots":
        raise ValueError(
            f"destroy/reset target must be under .orthos/chroots/: {resolved}"
        )

    # Must not be .orthos/chroots/ itself.
    if resolved.name == "chroots":
        raise ValueError(
            f"destroy/reset target must not be the chroots/ directory itself: {resolved}"
        )

    # Final component must match a safe <suite>-<arch> pattern.
    _SUITE_ARCH_RE = _re.compile(r"^[a-z0-9.+-]+-[a-z0-9_]+$")
    if not _SUITE_ARCH_RE.match(resolved.name):
        raise ValueError(
            f"destroy/reset target name {resolved.name!r} does not match the "
            f"expected <suite>-<arch> pattern (e.g. 'trixie-amd64'): {resolved}"
        )

    return resolved



def _validate_convergence_work_dir(path: Path) -> Path:
    """Return the resolved convergence work path if it passes validation.

    The convergence work tree lives at:
        .orthos/chroot-work/<suite>-<arch>/<repo-name>/build-convergence/
        or any parent level down to:
        .orthos/chroot-work/<suite>-<arch>/<repo-name>/

    Checks:
      - Must be absolute.
      - Must contain /.orthos/ as a path component.
      - Must have 'chroot-work' immediately after '.orthos'.
      - Must be at least 2 levels below 'chroot-work' (prevents targeting
        .orthos/chroot-work/ or .orthos/chroot-work/<suite>-<arch>/ alone).
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve convergence work path: {exc}") from exc

    if not resolved.is_absolute():
        raise ValueError(f"convergence work path must be absolute: {resolved}")

    parts = resolved.parts
    try:
        orthos_idx = parts.index(".orthos")
    except ValueError:
        raise ValueError(
            f"convergence work path must be under .orthos/: {resolved}"
        )

    if orthos_idx + 1 >= len(parts) or parts[orthos_idx + 1] != "chroot-work":
        raise ValueError(
            f"convergence work path must be under .orthos/chroot-work/: {resolved}"
        )

    # Need at least: .orthos / chroot-work / <suite>-<arch> / <repo>
    # i.e. at least 2 components after chroot-work.
    chroot_work_idx = orthos_idx + 1
    depth_after_chroot_work = len(parts) - chroot_work_idx - 1
    if depth_after_chroot_work < 2:
        raise ValueError(
            f"convergence work path must be at least 2 levels below "
            f".orthos/chroot-work/ (got depth {depth_after_chroot_work}): {resolved}"
        )

    return resolved



def _validate_bind_dst(root: Path, dst: Path) -> Path:
    """Ensure a bind-mount destination is inside the validated chroot root."""
    resolved_root = _validate_chroot_root(root)
    try:
        resolved_dst = Path(os.path.normpath(dst))
    except Exception as exc:
        raise ValueError(f"invalid mount destination: {exc}") from exc
    try:
        resolved_dst.relative_to(resolved_root)
    except ValueError:
        raise ValueError(
            f"mount destination {dst} is not inside chroot root {resolved_root}"
        )
    return resolved_dst


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def _ok(result: object = None) -> None:
    print(json.dumps({"ok": True, "result": result}), flush=True)

def _fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}), flush=True)

def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Internal privileged helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], step: str, log_fh: object = None) -> None:
    """Run *cmd*, optionally appending to *log_fh*. Raises RuntimeError on failure."""
    _log(f"orthos-priv: {step}")
    if log_fh:
        log_fh.write(f"\n# {step}\n$ {' '.join(cmd)}\n")  # type: ignore[union-attr]
        log_fh.flush()  # type: ignore[union-attr]
    result = subprocess.run(
        cmd,
        stdout=log_fh,
        stderr=subprocess.STDOUT if log_fh else None,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"step '{step}' failed (exit {result.returncode})"
        )


def _is_mounted(path: Path) -> bool:
    """Return True if *path* appears as a mount point in /proc/mounts."""
    path_str = str(path)
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == path_str:
                    return True
    except OSError:
        pass
    return False


def _mount_bind(src: str | Path, dst: Path, read_only: bool = False) -> None:
    """Bind-mount *src* to *dst*. Raises RuntimeError on failure.

    For read-only bind mounts, uses a two-step approach:
      1. mount --bind <src> <dst>
      2. mount -o bind,remount,ro <dst>
    Both steps must succeed; failure at either raises RuntimeError.
    """
    dst.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mount", "--bind", str(src), str(dst)],
        check=False,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise RuntimeError(f"failed to bind-mount {src} -> {dst}: {err}")
    _log(f"orthos-priv: mount --bind {src} -> {dst}")
    if read_only:
        ro_result = subprocess.run(
            ["mount", "-o", "bind,remount,ro", str(dst)],
            check=False,
            stderr=subprocess.PIPE,
            text=True,
        )
        if ro_result.returncode != 0:
            err = (ro_result.stderr or "").strip()
            # The bind mount succeeded but the remount failed.
            # Attempt to undo the bind mount before raising so the caller
            # does not have to guess what was partially done.
            subprocess.run(["umount", str(dst)], check=False)
            raise RuntimeError(
                f"failed to remount {dst} read-only (exit {ro_result.returncode}): {err}"
            )
        _log(f"orthos-priv: remount ro {dst}")


def _mount_special(fstype: str, dst: Path) -> None:
    """Mount a special filesystem (*fstype*) at *dst*. Raises RuntimeError on failure."""
    dst.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["mount", "-t", fstype, fstype, str(dst)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to mount {fstype} at {dst}")
    _log(f"orthos-priv: mount {fstype} -> {dst}")


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

def _op_create_chroot(args: dict) -> None:
    root = _validate_chroot_root(Path(args["root"]))
    suite = str(args.get("suite", _DEFAULT_SUITE))
    mirror = str(args.get("mirror", _DEBIAN_MIRROR))
    log_file_path: str | None = args.get("log_file")

    root.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_file_path, "w", encoding="utf-8") if log_file_path else None  # noqa: WPS515

    try:
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

        # Step 3: Bodhi apt source (explicit, not copied from host)
        bodhi_list = root / "etc" / "apt" / "sources.list.d" / "bodhi.list"
        _log(f"orthos-priv: writing Bodhi source -> {bodhi_list}")
        if log_fh:
            log_fh.write(f"\n# Bodhi source injection\n{_BODHI_SOURCE_LINE}\n")
            log_fh.flush()
        bodhi_list.parent.mkdir(parents=True, exist_ok=True)
        bodhi_list.write_text(_BODHI_SOURCE_LINE + "\n", encoding="utf-8")

        # Step 4: Bodhi keyring
        chroot_keyring_dir = root / "usr" / "share" / "keyrings"
        if _BODHI_KEYRING_HOST.exists():
            _log("orthos-priv: copying Bodhi keyring")
            if log_fh:
                log_fh.write(f"\n# Bodhi keyring: {_BODHI_KEYRING_HOST}\n")
                log_fh.flush()
            chroot_keyring_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "cp",
                    str(_BODHI_KEYRING_HOST),
                    str(chroot_keyring_dir / _BODHI_KEYRING_HOST.name),
                ],
                check=True,
            )
        else:
            raise RuntimeError(
                f"Bodhi keyring not found at {_BODHI_KEYRING_HOST}. "
                "Install bodhi-archive-keyring or ensure the keyring file exists."
            )

        # Step 5: apt-get update (picks up Bodhi source)
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
    "destroy-chroot":             _op_destroy_chroot,
    "reset-chroot":               _op_reset_chroot,
    "destroy-convergence-work":   _op_destroy_convergence_work,
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
