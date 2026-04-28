"""Debian package build backend: build from repo debian/ and retain artifacts."""

import shutil
from pathlib import Path
from typing import Any

from deb.backends.build_backend_meson import _clean_env
from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import info
from deb.utils.shell import run_logged

_RESULT_FILE = "build-result.json"
_ARTIFACT_GLOBS = ("*.deb", "*.changes", "*.buildinfo")

# Transient directories removed from the orthos workspace after a successful build.
_TRANSIENT_DIRS = ("stage",)


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


def _cleanup_transient(orthos: Path) -> None:
    """Remove transient workspace directories after a successful build."""
    for name in _TRANSIENT_DIRS:
        target = orthos / name
        if target.exists():
            shutil.rmtree(target)


def build(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run dpkg-buildpackage using debian/ from the source repo."""
    repo = Path(meta["repo_path"])
    orthos = orthos_dir(repo)

    dest_debian = repo / "debian"
    if not dest_debian.exists():
        raise FileNotFoundError(
            f"repo debian/ not found: {dest_debian}\n"
            f"Run 'orthos-packager apply {repo}' first."
        )

    logs_dir = orthos / "logs"
    ensure_dir(logs_dir)
    log_file = logs_dir / "build.log"
    log_file.write_text("", encoding="utf-8")  # truncate on each run

    info("using repo debian/ for build")

    ok, _ = run_logged(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        log_file=log_file,
        cwd=repo,
        env=_clean_env(),
    )

    if ok:
        artifacts = _retain_artifacts(repo, orthos)
        _cleanup_transient(orthos)
    else:
        artifacts = []

    result: dict[str, Any] = {
        "artifacts": artifacts,
        "artifacts_dir": str(orthos / "artifacts") if ok else None,
        "generated_debian_dir": str(orthos / "debian"),
        "target_debian_dir": str(dest_debian),
        "used_repo_debian": True,
        "used_generated_debian": False,
        "failure_step": None if ok else "dpkg-buildpackage",
        "log_file": str(log_file),
        "repo_path": str(repo),
        "success": ok,
    }

    write_json(orthos / _RESULT_FILE, result)
    return (0 if ok else 1), result
    