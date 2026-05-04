"""Write Debian maintainer scripts for orthos-generated packages."""

import stat
from pathlib import Path
from typing import Any

from deb.utils.log import info


# Maintainer script files that may be emitted when content is explicitly
# provided in meta["maintainer_scripts"].
_MAINTAINER_SCRIPTS = ("postinst", "preinst", "prerm", "postrm")


def _write_maintainer_scripts(
    debian_dir: Path,
    output_packages: list[dict[str, Any]],
    meta: dict[str, Any],
    write_text_fn: Any,
) -> None:
    """Write maintainer scripts from meta["maintainer_scripts"]."""
    scripts: dict[str, str] = meta.get("maintainer_scripts") or {}
    for name in _MAINTAINER_SCRIPTS:
        content = scripts.get(name, "").strip()
        if not content:
            continue
        script_path = debian_dir / name
        write_text_fn(script_path, content + "\n")
        script_path.chmod(0o755)

    for pkg in output_packages:
        pkg_name = pkg["name"]
        special_files = pkg.get("special_files", [])
        if not special_files:
            continue

        lines = [
            "#!/bin/sh",
            "set -e",
            "",
        ]

        for spec in special_files:
            path = spec["path"]
            mode_oct = int(spec["mode_octal"], 8)
            mode_str = spec["mode_octal"].replace("0o", "")
            owner = spec["owner"]
            group = spec["group"]

            if mode_oct & (stat.S_ISUID | stat.S_ISGID):
                owner = "root"
                group = "root"

            if owner == "root" and group == "root":
                lines.append(f"chown root:root {path}")
            elif owner or group:
                lines.append(f"chown {owner}:{group} {path}")

            lines.append(f"chmod {mode_str} {path}")
            info(f"  preserved special permission: {pkg_name} {path} {mode_str} {owner}:{group}")

        lines.append("")
        lines.append("#DEBHELPER#")
        lines.append("exit 0")
        lines.append("")

        script_path = debian_dir / f"{pkg_name}.postinst"
        write_text_fn(script_path, "\n".join(lines))
        script_path.chmod(0o755)
