"""Chroot environment lifecycle management for Orthos packaging.

ChrootEnv manages:
  - debootstrap-based rootfs creation using the configured suite/mirror
    (currently Debian trixie by default)
  - post-setup coordination: DNS, optional target repo profile injection,
    and base package installation
  - bind-mount setup and teardown

Separation of concerns:
  - ChrootEnv owns the chroot filesystem lifecycle and mount lifecycle.
  - ChrootRunner (runner.py) owns command execution inside the chroot.
  - run_convergence_loop (convergence.py) is filesystem-lifecycle agnostic.

Privilege boundary:
  All root-required operations are delegated to the privileged helper via
  deb.privileged.client. ChrootEnv contains no direct sudo subprocess calls.
  The helper performs path validation and operation allowlisting.

Mount lifecycle:
  - setup_mounts() / teardown_mounts() are called by the CLI, not by
    ChrootRunner or convergence logic.
  - The CLI must call teardown_mounts() in a finally block as the primary
    cleanup guarantee.
  - atexit is registered on the first setup_mounts() call as a safety net only.
    Do not rely on atexit as the primary cleanup mechanism.

Target repo profiles:
  Optional target repo profile sources and keyrings are injected explicitly
  into the chroot during creation. They are not copied from the host
  sources.list. This keeps the target package universe deterministic and
  auditable regardless of host configuration.

Bind-mount tradeoff:
  Build and log directories are bind-mounted from the host into the chroot.
  Package state is isolated inside the chroot; workspace paths are shared.
  This is a known, accepted practical compromise.

Generality note:
  Target repo profiles are package-universe overlays. ChrootEnv coordinates
  their use during chroot creation, while deb.target_repos defines the profile
  metadata and orthos-priv performs the privileged filesystem changes. The
  convergence engine itself remains general-purpose.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from deb.privileged import client
from deb.privileged.launcher import PrivilegedHelperError
from deb.utils.log import info


# ---------------------------------------------------------------------------
# Constants - environment-specific layer
# ---------------------------------------------------------------------------

_DEFAULT_SUITE = "trixie"
_DEBIAN_MIRROR = "http://deb.debian.org/debian"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ChrootEnvError(RuntimeError):
    """Raised when chroot creation, setup, or a mount operation fails."""


# ---------------------------------------------------------------------------
# ChrootEnv
# ---------------------------------------------------------------------------


class ChrootEnv:
    """Manage a debootstrap-based chroot for isolated convergence.

    Location:
      <chroot_root> - typically .orthos/<repo>/chroot/

    Reuse policy:
      The chroot is considered valid when <chroot_root>/bin/bash exists.
      Packages installed by previous convergence runs accumulate across runs
      (the chroot behaves like a real system between packaging runs).
      To reset: run 'orthos reset-chroot <repo>' or pass
      refresh=True to ensure_ready().

    Disk cost:
      ~400 MB for a minbase install with base packages. Per-repo chroots
      prevent cross-repo package contamination.
    """

    def __init__(self, chroot_root: Path) -> None:
        self._root = chroot_root
        self._mounts: list[Path] = []     # active mounts in setup order
        self._atexit_registered = False

    @property
    def root(self) -> Path:
        return self._root

    def exists(self) -> bool:
        """Return True when the chroot rootfs appears complete."""
        return (self._root / "bin" / "bash").exists()

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def create(
        self,
        suite: str = _DEFAULT_SUITE,
        repo_set: str | None = None,
        mirror: str = _DEBIAN_MIRROR,
        log_file: Path | None = None,
    ) -> None:
        """Create the chroot rootfs via debootstrap and run post-setup.

        Delegates all root-required steps to the privileged helper:
          1. debootstrap --variant=minbase <suite> <root> <mirror>
          2. Copy /etc/resolv.conf into chroot (DNS)
          3. Write target repo profile apt source (if the profile includes one)
          4. Copy target repo profile keyring (if the profile includes one)
          5. apt-get update inside chroot
          6. apt-get install -y <base packages> inside chroot

        *repo_set* is None (native target profile, indicating standard distro
        sources with no explicit repository overlay, rather than copying host
        apt sources) by default. Pass "debian" or "debodhi" to select
        an explicit target package universe.
        All output is appended to log_file (if provided).
        Raises ChrootEnvError on any step failure.
        """

        info(
            f"convergence: chroot: creating {self._root} "
            f"(suite={suite}, mirror={mirror})"
        )
        try:
            client.create_chroot(
                root=self._root,
                suite=suite,
                repo_set=repo_set,
                mirror=mirror,
                log_file=log_file,
            )
        except PrivilegedHelperError as exc:
            raise ChrootEnvError(str(exc)) from exc
        info(f"convergence: chroot: created {self._root}")

    def ensure_ready(
        self,
        suite: str = _DEFAULT_SUITE,
        repo_set: str | None = None,
        mirror: str = _DEBIAN_MIRROR,
        refresh: bool = False,
        log_file: Path | None = None,
    ) -> None:
        """Create the chroot if absent; recreate if refresh=True; else reuse.

        *repo_set* is None (native target profile, indicating standard distro
        sources with no explicit repository overlay, rather than copying host
        apt sources) by default. Pass "debian" or "debodhi" to select
        an explicit target package universe.
        Raises ChrootEnvError if creation fails.
        """
        if refresh and self._root.exists():
            info(f"convergence: chroot: --refresh-chroot: resetting {self._root}")
            try:
                client.reset_chroot(self._root)
            except PrivilegedHelperError as exc:
                raise ChrootEnvError(str(exc)) from exc

        if self.exists():
            info(f"convergence: chroot: reusing {self._root}")
        else:
            self.create(suite=suite, repo_set=repo_set, mirror=mirror, log_file=log_file)

    # ------------------------------------------------------------------
    # Mount lifecycle
    # ------------------------------------------------------------------

    def setup_mounts(
        self,
        source_repo: Path,
        build_dir: Path,
        logs_dir: Path,
        build_src: Path | None = None,
    ) -> None:
        """Bind-mount proc/dev/sys/source/build/logs into the chroot.

        Must be called by the CLI before creating a ChrootRunner.
        The caller is responsible for calling teardown_mounts() in a
        finally block (primary cleanup guarantee).

        Also registers an atexit handler as a backup safety net on the first
        call. atexit alone is not a sufficient cleanup guarantee.

        Raises ChrootEnvError if any mount fails.
        """
        if not self._atexit_registered:
            atexit.register(self.teardown_mounts)
            self._atexit_registered = True

        info(f"convergence: chroot: setup-mounts start: {self._root}")
        try:
            mounted = client.setup_mounts(
                root=self._root,
                source_repo=source_repo,
                build_dir=build_dir,
                logs_dir=logs_dir,
                build_src=build_src,
            )
        except PrivilegedHelperError as exc:
            raise ChrootEnvError(str(exc)) from exc

        self._mounts = mounted
        info(f"convergence: chroot: setup-mounts done: {len(mounted)} mounts")

    def teardown_mounts(self) -> None:
        """Unmount all tracked mounts in reverse order.

        Does not raise. Logs failures and continues unmounting remaining paths.

        Primary cleanup path: called explicitly in a finally block by the CLI.
        Backup: registered as an atexit handler by setup_mounts(). The atexit
        registration does NOT make the explicit finally block optional.
        """
        if not self._mounts:
            return
        info(f"convergence: chroot: teardown-mounts: {len(self._mounts)} mounts")
        try:
            client.teardown_mounts(root=self._root, mounts=self._mounts)
        except PrivilegedHelperError as exc:
            info(f"convergence: chroot: WARNING - teardown-mounts error: {exc}")
        # Clear the list regardless - we do not want stale entries on retry.
        self._mounts = []
