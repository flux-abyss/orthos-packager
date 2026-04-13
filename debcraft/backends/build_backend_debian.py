"""Debian package build backend: copy generated debian/, run dpkg-buildpackage."""

import shutil
from pathlib import Path
from typing import Any

from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.shell import run_logged

_RESULT_FILE = "build-result.json"
_ARTIFACT_GLOBS = ("*.deb", "*.changes", "*.buildinfo")


def _orthos_dir(repo_path: Path) -> Path:
    """Mirror the layout used by all earlier steps."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def _collect_artifacts(parent: Path) -> list[str]:
    """Return sorted absolute paths for all .deb/.changes/.buildinfo in *parent*."""
    found: list[Path] = []
    for pattern in _ARTIFACT_GLOBS:
        found.extend(parent.glob(pattern))
    return sorted(str(p.resolve()) for p in found)


def build(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Copy generated debian/ into the repo and run dpkg-buildpackage.

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

    # Remove any existing debian/ in the target repo.
    if dest_debian.exists():
        shutil.rmtree(dest_debian)

    shutil.copytree(src_debian, dest_debian)

    ok, _ = run_logged(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        log_file=log_file,
        cwd=repo,
    )

    failure_step: str | None = None if ok else "dpkg-buildpackage"
    artifacts = _collect_artifacts(repo.parent) if ok else []

    result: dict[str, Any] = {
        "artifacts": artifacts,
        "generated_debian_dir": str(src_debian),
        "target_debian_dir": str(dest_debian),
        "failure_step": failure_step,
        "log_file": str(log_file),
        "repo_path": str(repo),
        "success": ok,
    }

    write_json(orthos / _RESULT_FILE, result)
    return (0 if ok else 1), result
