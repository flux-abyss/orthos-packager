"""stage command handler."""

import subprocess

from deb.backends.build_backend_meson import stage as meson_stage
from deb.utils.log import error, info


def cmd_stage(repo_path: str, probe, meson_options: dict[str, str] | None = None) -> int:
    """Run the Meson staging pipeline for a repository."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    if meson_options:
        meta["meson_options"] = meson_options

    info(f"staging: {meta['repo_path']}")
    info("running meson setup …")

    rc, result = meson_stage(meta)

    if rc == 0:
        info(f"project: {result['project_name'] or '(unknown)'}  "
             f"version: {result['version'] or '(unknown)'}")
        info(f"stage:   {result['stage_dir']}")
        info(f"log:     {result['log_file']}")
        info("result:  success")
    else:
        step = result.get("failure_step", "unknown step")
        error(f"staging failed at: {step}")
        error(f"see log: {result['log_file']}")

        for verdict in result.get("expert_verdicts", []):
            info(f"expert:  [{verdict['rule_id']}] "
                 f"confidence={verdict['confidence']:.0%}")
            info(f"         {verdict['summary']}")
            info(f"         action: {verdict['suggested_action']}")

        if result.get("next_mode") == "compatibility_search":
            info("next:    compatibility search mode")
            info("prefer:  an older release/tag before more dependency resolution")
            sp = result.get("symbol_provider")
            if sp:
                info(f"symbol:  {sp['symbol']}")
                info(f"header:  {sp['header']}")
                info(f"inferred: {sp['package']}")
            tvi = result.get("target_version_info")
            if tvi:
                info(f"target:  {tvi['package']} = {tvi['package_version'] or '(not installed)'}")
                info(f"pc:      {tvi['pkgconfig_module']} = {tvi['pkgconfig_version'] or '(not found)'}")

                pc_ver: str = tvi.get("pkgconfig_version") or ""
                parts = pc_ver.split(".")
                if len(parts) >= 2:
                    major_minor = f"{parts[0]}.{parts[1]}"
                    info(f"hint:    upstream versions near {major_minor}.x are likely compatible")

                    try:
                        tag_result = subprocess.run(
                            ["git", "-C", repo_path, "tag"],
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=5,
                        )
                        if tag_result.returncode == 0:
                            matching = [
                                t for t in tag_result.stdout.splitlines()
                                if t.startswith(major_minor)
                            ]
                            if matching:
                                info("suggest:")
                                for tag in sorted(matching, reverse=True)[:5]:
                                    info(f"  {tag}")
                            else:
                                info(f"suggest: no local tags found matching {major_minor}.*")
                    except (OSError, subprocess.TimeoutExpired):
                        pass

    return rc
