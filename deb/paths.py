"""Canonical path helpers shared across all pipeline stages."""

from pathlib import Path

# Architecture assumed for the shared chroot.  Extend to detect dynamically
# when multi-arch support is needed.
_CHROOT_ARCH = "amd64"


def orthos_dir(repo_path: Path) -> Path:
    """Return the .orthos workspace directory for a repository."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def shared_chroot_dir(suite: str) -> Path:
    """Return the shared chroot directory for *suite* and the host arch.

    Layout:  .orthos/chroots/<suite>-<arch>/

    The shared chroot lives outside every per-project workspace so that
    per-project directories (.orthos/<repo>/) contain only user-owned files
    and can be removed with plain 'rm -rf' without requiring sudo.

    The chroot itself may be root-owned (created by orthos-priv); that is
    expected and acceptable.
    """
    base = Path.cwd() / ".orthos" / "chroots"
    return base / f"{suite}-{_CHROOT_ARCH}"


def shared_convergence_build_dir(suite: str, repo_name: str) -> Path:
    """Return the Meson convergence build directory for *suite* and *repo_name*.

    Layout:  .orthos/chroot-work/<suite>-<arch>/<repo-name>/build-convergence/

    This directory is bind-mounted into the shared chroot as /orthos/build
    during convergence.  Because Meson runs as root inside the chroot, the
    files it creates here are root-owned.  Placing this tree under
    .orthos/chroot-work/ (not under .orthos/<repo>/) means the per-project
    workspace (.orthos/<repo>/) contains only user-owned files and can be
    removed with plain 'rm -rf' without requiring sudo.

    The chroot-work tree is cleaned by reset-chroot via orthos-priv.
    """
    base = Path.cwd() / ".orthos" / "chroot-work"
    return base / f"{suite}-{_CHROOT_ARCH}" / repo_name / "build-convergence"

def shared_stage_build_dir(suite: str, repo_name: str) -> Path:
    """Return the Meson staging build directory for *suite* and *repo_name*.

    Layout:  .orthos/chroot-work/<suite>-<arch>/<repo-name>/build-stage/

    This directory is bind-mounted into the shared chroot as /orthos/build
    during the staging phase (separate from the convergence build dir so
    convergence state is never clobbered).  Because Meson runs as root inside
    the chroot, files written here are root-owned; the CLI reads them back
    from the host-side path after mount teardown.
    """
    base = Path.cwd() / ".orthos" / "chroot-work"
    return base / f"{suite}-{_CHROOT_ARCH}" / repo_name / "build-stage"


