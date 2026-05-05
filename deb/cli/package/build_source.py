"""Source-copy helpers for the orthos package command."""

import shutil
from pathlib import Path


# Directories excluded from the isolated package source copy.
_BUILD_SRC_EXCLUDE = {".git", ".orthos", "build", "dist", "__pycache__", "debian"}


def prepare_build_source(repo_path: Path, orthos_path: Path) -> Path:
    """Create an isolated copy of *repo_path* under *orthos_path*/build-src/.

    Any previous build-src is removed before recreation so the copy is always
    fresh.  Only .git, .orthos, build, dist, __pycache__, and debian are
    excluded; all other source files are preserved verbatim.

    Returns the path to the new build-src directory.
    """
    build_src = orthos_path / "build-src"
    if build_src.exists():
        shutil.rmtree(build_src)

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
