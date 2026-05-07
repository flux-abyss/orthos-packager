"""Meson build-backend adapter for the orthos backend registry."""

# _CARGO_ENV is defined here (not in chroot.py) so that stage_chroot() can
# reference it without creating a backend → cli circular import.

import re
import subprocess
from pathlib import Path
from typing import Any

import deb.backends.build_backend_meson as _impl

name = "meson"

# Environment prefix injected into every chroot Meson invocation.
# Cargo (if used by the project) needs a writable target directory;
# /orthos/source is read-only, so we redirect Cargo output here.
_CARGO_ENV = "CARGO_TARGET_DIR=/orthos/build/cargo-target"

# Matches: project('name', ...) or project("name", ...)
_RE_PROJECT_NAME = re.compile(r"""project\s*\(\s*['"]([^'"]+)['"]""")

# Matches: version: '1.2.3' or version: "1.2.3"
_RE_VERSION = re.compile(r"""version\s*:\s*['"]([^'"]+)['"]""")


def can_handle(repo: Path) -> bool:
    """Return True when *repo* contains a meson.build file."""
    return (repo / "meson.build").exists()


def _parse_meson_build(meson_file: Path) -> tuple[str | None, str | None]:
    """Return (name, version) parsed from meson.build, or (None, None)."""
    try:
        text = meson_file.read_text(encoding="utf-8")
    except OSError:
        return None, None

    name_match = _RE_PROJECT_NAME.search(text)
    version_match = _RE_VERSION.search(text)

    project_name = name_match.group(1) if name_match else None
    version = version_match.group(1) if version_match else None
    return project_name, version


def _git_version(repo: Path) -> str | None:
    """Return a version string from the nearest git tag, or None.

    Uses 'git describe --tags --abbrev=0' which returns the nearest
    ancestor tag with no suffix.  Strips a leading 'v' prefix.
    Only called when meson.build has no version field.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "describe", "--tags", "--abbrev=0"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag.lstrip("v") if tag else None


def scan_metadata(repo: Path) -> dict[str, Any]:
    """Return Meson-specific metadata for *repo*.

    Keys returned:
        build_backend  - always "meson"
        meson          - always True (legacy compat flag)
        project_name   - parsed from meson.build project() call, or None
        version        - from meson.build, git tag, or None
        version_source - "meson" | "git-tag" | "fallback"
    """
    project_name, version = _parse_meson_build(repo / "meson.build")

    # Version precedence:
    #   1. meson.build project() version: field
    #   2. nearest ancestor git tag (stripped of leading 'v')
    #   3. None  (generator will apply its own _VERSION_FALLBACK)
    version_source = "meson"
    if not version:
        version = _git_version(repo)
        version_source = "git-tag" if version else "fallback"

    return {
        "build_backend": name,
        "meson": True,
        "project_name": project_name,
        "version": version,
        "version_source": version_source,
    }


def stage(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run the Meson staging flow described by *meta*.

    Delegates entirely to the existing implementation so behaviour is
    preserved exactly.
    """
    return _impl.stage(meta)


def stage_deps() -> list[str]:
    """Return stage-time packages for Meson projects.

    Meson and Ninja are installed by the convergence loop, so no extra
    packages are needed here.  Returns an empty list.
    """
    return []


def stage_chroot(
    meta: dict[str, Any],
    chroot_exec_fn,
    chroot_root: "Path",
    source_path: str,
    build_path: str,
    destdir_path: str,
    log_file: "Path",
) -> tuple[bool, str]:
    """Run meson setup/compile/install inside an already-mounted chroot.

    Arguments:
        meta           - probe metadata dict (may contain meson_options).
        chroot_exec_fn - callable(chroot_root, cmd) -> (bool, str) from
                         deb.privileged.client.chroot_exec.
        chroot_root    - host-side Path to the chroot root.
        source_path    - chroot-internal source path ("/orthos/source").
        build_path     - chroot-internal build path ("/orthos/build").
        destdir_path   - chroot-internal DESTDIR ("/orthos/build/destdir").
        log_file       - host-side Path; output is appended per step.

    Returns:
        (True, "") on success, or (False, failure_step_name) on the first
        failed step.
    """
    import shlex  # noqa: PLC0415 — stdlib, lightweight

    meson_options: dict[str, str] = meta.get("meson_options") or {}
    flags = [f"-D{k}={v}" for k, v in sorted(meson_options.items())]

    steps: list[tuple[str, list[str]]] = [
        (
            "meson setup",
            [
                "bash", "-c",
                shlex.join([
                    _CARGO_ENV,
                    "meson", "setup",
                    build_path, source_path,
                    "--prefix=/usr", "--sysconfdir=/etc",
                    "--localstatedir=/var", "--libdir=lib/x86_64-linux-gnu",
                    *flags,
                ]),
            ],
        ),
        (
            "meson compile",
            [
                "bash", "-c",
                shlex.join([_CARGO_ENV, "meson", "compile", "-C", build_path]),
            ],
        ),
        (
            "meson install",
            [
                "bash", "-c",
                f"DESTDIR={destdir_path} meson install -C {build_path}",
            ],
        ),
    ]

    for step_name, cmd in steps:
        ok, output = chroot_exec_fn(chroot_root, cmd)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# {step_name}\n{output}")
        if not ok:
            return False, step_name

    return True, ""
