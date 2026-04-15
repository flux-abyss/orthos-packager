"""Debian package build backend: copy generated debian/, run dpkg-buildpackage."""

import shutil
from pathlib import Path
from typing import Any

from debcraft.backends.build_backend_meson import _clean_env
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import info
from debcraft.utils.shell import run_logged

_RESULT_FILE = "build-result.json"
_ARTIFACT_GLOBS = ("*.deb", "*.changes", "*.buildinfo")

# Transient directories removed from the orthos workspace after a successful build.
_TRANSIENT_DIRS = ("stage",)


def _orthos_dir(repo_path: Path) -> Path:
    """Return the orthos workspace directory for *repo_path*."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def _collect_from_parent(repo: Path) -> list[Path]:
    """Return all artifact paths emitted by dpkg-buildpackage into repo.parent."""
    found: list[Path] = []
    for pattern in _ARTIFACT_GLOBS:
        found.extend(repo.parent.glob(pattern))
    return sorted(found)


def _retain_artifacts(repo: Path, orthos: Path) -> list[str]:
    """Move build artifacts into orthos/artifacts and return their paths."""
    artifacts_dir = orthos / "artifacts"
    ensure_dir(artifacts_dir)

    retained: list[str] = []
    for src in _collect_from_parent(repo):
        dest = artifacts_dir / src.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), dest)
        retained.append(str(dest.resolve()))
        info(f"artifact: {dest}")

    return sorted(retained)


def _cleanup_transient(orthos: Path, dest_debian: Path) -> None:
    """Remove transient workspace directories after a successful build."""
    for name in _TRANSIENT_DIRS:
        target = orthos / name
        if target.exists():
            shutil.rmtree(target)

    if dest_debian.exists():
        shutil.rmtree(dest_debian)


def build(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Copy generated debian/ into the repo, run dpkg-buildpackage, and  artifacts.

    Returns (exit_code, result_dict).
    Raises FileNotFoundError if the generated debian/ skeleton is missing.
    """
    repo = Path(meta["repo_path"])
    orthos = _orthos_dir(repo)

    src_debian = orthos / "debian"
    if not src_debian.exists():
        raise FileNotFoundError(f"generated debian/ not found: {src_debian}\n"
                                f"Run 'orthos-packager generate {repo}' first.")

    logs_dir = orthos / "logs"
    ensure_dir(logs_dir)
    log_file = logs_dir / "build.log"
    log_file.write_text("", encoding="utf-8")  # truncate on each run

    dest_debian = repo / "debian"

    if dest_debian.exists():
        shutil.rmtree(dest_debian)

    shutil.copytree(src_debian, dest_debian)

    ok, _ = run_logged(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        log_file=log_file,
        cwd=repo,
        env=_clean_env(),
    )

    if ok:
        artifacts = _retain_artifacts(repo, orthos)
        _cleanup_transient(orthos, dest_debian)
    else:
        artifacts = []

    result: dict[str, Any] = {
        "artifacts": artifacts,
        "artifacts_dir": str(orthos / "artifacts") if ok else None,
        "generated_debian_dir": str(src_debian),
        "target_debian_dir": str(dest_debian),
        "failure_step": None if ok else "dpkg-buildpackage",
        "log_file": str(log_file),
        "repo_path": str(repo),
        "success": ok,
    }

    write_json(orthos / _RESULT_FILE, result)
    return (0 if ok else 1), result
