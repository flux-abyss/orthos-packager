"""Materialize generated debian/ from the orthos workspace into the source repo."""

import shutil
from pathlib import Path
from typing import Any

from debcraft.paths import orthos_dir
from debcraft.utils.fs import write_json
from debcraft.utils.log import info

_RESULT_FILE = "apply-result.json"


def apply(meta: dict[str, Any],
          force: bool = False) -> tuple[int, dict[str, Any]]:
    """Copy .orthos/<repo>/debian into <repo>/debian.

    Raises FileNotFoundError if the generated debian/ does not exist.
    Raises FileExistsError if the destination exists and force is False.
    """
    repo = Path(meta["repo_path"])
    orthos = orthos_dir(repo)

    src = orthos / "debian"
    dest = repo / "debian"

    if not src.exists():
        raise FileNotFoundError(f"generated debian/ not found: {src}\n"
                                f"Run 'orthos-packager generate {repo}' first.")

    overwritten = False
    info("applying generated debian/ to repo")

    if dest.exists():
        info("destination exists")
        if not force:
            raise FileExistsError(f"destination already exists: {dest}\n"
                                  f"Use --force to overwrite.")
        info("force overwrite enabled")
        shutil.rmtree(dest)
        overwritten = True

    shutil.copytree(src, dest)
    info(f"applied to: {dest}")

    result: dict[str, Any] = {
        "applied": True,
        "overwritten": overwritten,
        "repo_path": str(repo),
        "source_debian_dir": str(src),
        "target_debian_dir": str(dest),
    }

    write_json(orthos / _RESULT_FILE, result)
    return 0, result
