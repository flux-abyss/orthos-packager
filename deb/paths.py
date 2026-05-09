"""Canonical path helpers shared across all pipeline stages."""

from pathlib import Path

# Architecture assumed for the shared chroot.  Extend to detect dynamically
# when multi-arch support is needed.
_CHROOT_ARCH = "amd64"


def orthos_dir(repo_path: Path) -> Path:
    """Return the .orthos workspace directory for a repository."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def chroot_target_name(suite: str, target: str | None) -> str:
    """Return the canonical chroot directory key for *suite* and *target*.

    When *target* is None or empty, "native" is used so the naming is
    explicit and consistent regardless of how the caller obtained the value.

    Example: chroot_target_name("trixie", "debodhi") -> "trixie-debodhi"
             chroot_target_name("trixie", None)    -> "trixie-native"
    """
    return f"{suite}-{target or 'native'}"


def shared_chroot_dir(target_name: str) -> Path:
    """Return the shared chroot directory for *target_name* and the host arch.

    Layout:  .orthos/chroots/<target-name>-<arch>/

    The shared chroot lives outside every per-project workspace. Note that
    while the per-project workspace (.orthos/<repo>/) is mostly user-owned,
    certain steps (such as bind-mounted builds) might leave root-owned files;
    such root-owned leftovers are cleaned through orthos-priv where needed.

    The chroot itself may be root-owned (created by orthos-priv); that is
    expected and acceptable.
    """
    base = Path.cwd() / ".orthos" / "chroots"
    return base / f"{target_name}-{_CHROOT_ARCH}"


def shared_convergence_build_dir(target_name: str, repo_name: str) -> Path:
    """Return the Meson convergence build directory for *target_name* and *repo_name*.

    Layout:  .orthos/chroot-work/<target-name>-<arch>/<repo-name>/build-convergence/

    This directory is bind-mounted into the shared chroot as /orthos/build
    during convergence.  Because Meson runs as root inside the chroot, the
    files it creates here are root-owned. Placing this tree under
    .orthos/chroot-work/ (not under .orthos/<repo>/) isolates major root-owned
    build trees so they can be securely cleaned by reset-chroot via orthos-priv,
    keeping the per-project workspace (.orthos/<repo>/) primarily user-owned.

    The chroot-work tree is cleaned by reset-chroot via orthos-priv.
    """
    base = Path.cwd() / ".orthos" / "chroot-work"
    return base / f"{target_name}-{_CHROOT_ARCH}" / repo_name / "build-convergence"


def shared_stage_build_dir(target_name: str, repo_name: str) -> Path:
    """Return the Meson staging build directory for *target_name* and *repo_name*.

    Layout:  .orthos/chroot-work/<target-name>-<arch>/<repo-name>/build-stage/

    This directory is bind-mounted into the shared chroot as /orthos/build
    during the staging phase (separate from the convergence build dir so
    convergence state is never clobbered).  Because Meson runs as root inside
    the chroot, files written here are root-owned; the CLI reads them back
    from the host-side path after mount teardown.
    """
    base = Path.cwd() / ".orthos" / "chroot-work"
    return base / f"{target_name}-{_CHROOT_ARCH}" / repo_name / "build-stage"


def shared_runtime_smoke_chroot_dir(target_name: str) -> Path:
    """Return the path for an isolated runtime smoke validation chroot.

    Layout:  .orthos/chroots/<target-name>-<arch>-runtime-smoke/

    This chroot is intentionally separate from the build/convergence chroot.
    It starts clean so that missing runtime dependencies are NOT masked by
    build-time packages that happen to be installed in the build chroot.
    """
    base = Path.cwd() / ".orthos" / "chroots"
    return base / f"{target_name}-{_CHROOT_ARCH}-runtime-smoke"


