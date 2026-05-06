"""Source-copy helpers for the orthos package command."""

import shutil
from pathlib import Path

from deb.privileged.client import PrivilegedHelperError, destroy_build_src
from deb.utils.log import error, info


# Directories excluded from the isolated package source copy.
_BUILD_SRC_EXCLUDE = {".git", ".orthos", "build", "dist", "__pycache__", "debian"}


def _remove_build_src(build_src: Path) -> bool:
    """Remove *build_src*, using a privileged helper if user-space removal fails.

    Returns True on success, False if removal could not be completed.
    """
    try:
        shutil.rmtree(build_src)
        return True
    except PermissionError:
        info(
            f"package: build-src has root-owned files "
            f"(left by previous chroot build); using privileged cleanup: {build_src}"
        )
    try:
        destroy_build_src(build_src)
        return True
    except PrivilegedHelperError as exc:
        error(f"package: failed to remove stale build-src via privileged helper: {exc}")
        return False


def prepare_build_source(repo_path: Path, orthos_path: Path) -> Path:
    """Create an isolated copy of *repo_path* under *orthos_path*/build-src/.

    Any previous build-src is removed before recreation so the copy is always
    fresh.  Only .git, .orthos, build, dist, __pycache__, and debian are
    excluded; all other source files are preserved verbatim.

    If the previous build-src contains root-owned files (left by
    dpkg-buildpackage running inside the chroot), the privileged helper is
    used to remove it.

    Returns the path to the new build-src directory.
    Raises RuntimeError if an existing build-src cannot be removed.
    """
    build_src = orthos_path / "build-src"
    if build_src.exists():
        if not _remove_build_src(build_src):
            raise RuntimeError(
                f"package: cannot remove stale build-src at {build_src}; "
                "privileged cleanup failed; see orthos-priv output above"
            )

    def _ignore(src: str, names: list[str]) -> set[str]:
        return {n for n in names if n in _BUILD_SRC_EXCLUDE}

    shutil.copytree(repo_path, build_src, ignore=_ignore, dirs_exist_ok=False)
    return build_src


def copy_generated_debian_to_build_source(
    generated_debian: Path, build_src: Path
) -> None:
    """Copy *generated_debian* into *build_src*/debian/.

    Any previous build_src/debian is removed first so the injection is clean.
    """
    target = build_src / "debian"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(generated_debian, target, dirs_exist_ok=False)
