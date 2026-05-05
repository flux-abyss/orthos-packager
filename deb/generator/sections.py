"""Debian section mapping and primary section inference for orthos generator."""

from pathlib import Path
from typing import Any


# Bucket-to-section mapping for non-primary, non-GUI packages.
_BUCKET_SECTION: dict[str, str] = {
    "dev":     "devel",
    "doc":     "doc",
    "runtime": "libs",
}


def _infer_primary_section(plan_buckets: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    """Infer the primary Debian section from metadata and staged paths.

    Priority:
      1. meta["section"] override
      2. desktop/session evidence -> x11
      3. content-family path evidence -> specific section
      4. fallback -> misc
    """
    override = meta.get("section", "").strip()
    if override:
        return override

    all_paths = [
        path.lstrip("/")
        for bucket in plan_buckets
        for path in bucket.get("files", [])
    ]

    for path in all_paths:
        if path.endswith(".desktop") and (
            path.startswith("usr/share/applications/")
            or path.startswith("usr/share/xsessions/")
            or path.startswith("usr/share/wayland-sessions/")
        ):
            return "x11"

    def has_prefix(prefixes: tuple[str, ...]) -> bool:
        return any(path.startswith(p) for path in all_paths for p in prefixes)

    if has_prefix(("usr/share/fonts/", "usr/share/fontconfig/")):
        return "fonts"
    if has_prefix(("usr/share/sounds/", "usr/share/pulseaudio/", "usr/share/alsa/", "usr/lib/alsa-lib/")):
        return "sound"
    if has_prefix(("usr/share/icons/", "usr/share/pixmaps/", "usr/share/wallpapers/", "usr/share/backgrounds/", "usr/share/thumbnailers/")):
        return "graphics"

    for path in all_paths:
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "usr" and parts[1] == "lib":
            # Detect usr/lib/gstreamer-* or usr/lib/<triplet>/gstreamer-*
            if parts[2].startswith("gstreamer-") or (len(parts) >= 4 and parts[3].startswith("gstreamer-")):
                return "video"

    if has_prefix(("usr/share/webext/", "usr/share/javascript/", "usr/share/nginx/", "usr/share/apache2/")):
        return "web"
    if has_prefix(("usr/games/", "usr/share/games/")):
        return "games"

    for path in all_paths:
        if path.startswith("usr/share/mime/"):
            return "text"
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "usr" and parts[1] == "share" and parts[2].startswith("gtksourceview-"):
            return "text"

    if has_prefix(("usr/bin/", "usr/sbin/")):
        return "utils"

    return "misc"
