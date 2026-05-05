"""Path and request validation helpers for orthos-priv."""

from __future__ import annotations

import os
import re
from pathlib import Path


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
