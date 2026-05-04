"""build command handler."""

from deb.backends.build_backend_debian import build as run_build
from deb.utils.log import error, info


def cmd_build(repo_path: str, probe, meson_options: dict[str, str] | None = None) -> int:
    """Run dpkg-buildpackage using repo/debian."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    if meson_options:
        meta["meson_options"] = meson_options

    try:
        rc, result = run_build(meta)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1

    info(f"repo:    {result['repo_path']}")
    info(f"debian:  {result['target_debian_dir']}")
    info(f"log:     {result['log_file']}")

    if result["success"]:
        info("result:  success")
        info(f"artifacts: {len(result['artifacts'])}")
        for p in result["artifacts"]:
            info(f"  {p}")
    else:
        failure_step = result.get("failure_step") or "unknown"
        error(f"build failed at: {failure_step}")
        error(f"see log: {result['log_file']}")

    return rc
