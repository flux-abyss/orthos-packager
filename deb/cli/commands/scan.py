"""scan command handler."""

from pathlib import Path

from deb.paths import orthos_dir
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import error, info

_META_FILE = "package-meta.json"


def cmd_scan(repo_path: str, probe) -> int:
    """Run the scan command and write package metadata JSON."""
    try:
        meta = probe(repo_path)
    except (FileNotFoundError, NotADirectoryError, ValueError) as exc:
        error(str(exc))
        return 1

    out_dir = orthos_dir(Path(meta["repo_path"]))
    ensure_dir(out_dir)
    out_file = out_dir / _META_FILE
    write_json(out_file, meta)

    name = meta["project_name"] or "(unknown)"
    version = meta["version"] or "(unknown)"
    debian = "yes" if meta["debian_dir"] else "no"

    info(f"repo:    {meta['repo_path']}")
    info(f"project: {name}  version: {version}")
    info(f"debian/: {debian}")

    dc = meta.get("distro_candidate")
    if dc:
        info(f"distro:  {dc['package']} = {dc['candidate_version']}")
        parts = dc["candidate_version"].split(".")
        if len(parts) >= 2:
            anchor = f"{parts[0]}.{parts[1]}"
            info(f"anchor:  start from upstream ~{anchor} before compatibility guessing")
    else:
        info("distro:  (package not found in configured apt sources)")

    info(f"wrote:   {out_file}")

    return 0
