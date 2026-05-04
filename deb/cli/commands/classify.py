"""classify command handler."""

from deb.classifier.artifact_classifier import classify as run_classify
from deb.utils.log import error, info


def cmd_classify(repo_path: str, probe) -> int:
    """Group inventory entries into package buckets and write package-plan.json."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    try:
        rc, result = run_classify(meta)
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"inv:     {result['inventory_file']}")
    info(f"files:   {result['total_files']}")

    for bucket in result["package_buckets"]:
        info(f"  {bucket['name']:<10} {bucket['file_count']}")

    info(f"wrote:   {result['plan_file']}")
    return rc
