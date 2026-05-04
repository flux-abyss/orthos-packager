"""suggest command handler."""

from deb.suggest import suggest as run_suggest
from deb.utils.log import error, info


def cmd_suggest(repo_path: str, probe) -> int:
    """Read analyze-result.json and emit a structured suggestion."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        _rc, result, suggest_file = run_suggest(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

    status = "success" if result["success"] else "failure"
    info(f"repo:    {meta['repo_path']}")
    info(f"result:  {status}")

    if result["category"]:
        info(f"category: {result['category']}")

    if result["suggestion_type"]:
        info(f"type:     {result['suggestion_type']}")

    if result["target_file"]:
        info(f"target:   {result['target_file']}")

    if result["suggested_change"]:
        info(f"change:   {result['suggested_change']}")

    if result["next_step"]:
        info(f"next:     {result['next_step']}")

    if result["suggested_command"]:
        info(f"command:  {result['suggested_command']}")

    info(f"confidence: {result['confidence']}")
    info(f"wrote:   {suggest_file}")
    return 0
