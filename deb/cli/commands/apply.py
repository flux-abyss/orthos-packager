"""apply command handler."""

from deb.apply_debian import apply as run_apply
from deb.utils.log import error, info


def cmd_apply(repo_path: str, probe, force: bool = False) -> int:
    """Materialize generated debian/ from the orthos workspace into the repo."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result = run_apply(meta, force=force)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1
    except FileExistsError as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"source:  {result['source_debian_dir']}")
    info(f"target:  {result['target_debian_dir']}")
    if result["overwritten"]:
        info("overwritten: yes")
    info("result:  applied")
    return 0
