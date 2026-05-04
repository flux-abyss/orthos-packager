"""analyze command handler."""

from deb.analyze import analyze as run_analyze
from deb.utils.log import error, info


def cmd_analyze(repo_path: str, probe) -> int:
    """Read build-result.json and build.log and emit an analysis summary."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result, analyze_file = run_analyze(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

    status = "success" if result["success"] else "failure"
    info(f"repo:    {meta['repo_path']}")
    info(f"result:  {status}")

    if not result["success"]:
        info(f"category: {result['category']}")
        info(f"summary:  {result['summary']}")
        if result["log_excerpt"]:
            info("excerpt:")
            for line in result["log_excerpt"]:
                info(f"  {line}")
    else:
        info(f"summary:  {result['summary']}")

    info(f"wrote:   {analyze_file}")
    return 0
