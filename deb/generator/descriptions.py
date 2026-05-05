"""Package description templates and helpers for orthos generator."""

from typing import Any

# Bucket-based description templates: (short, long).
# The primary/collapsed case is handled separately.
_BUCKET_DESCRIPTIONS: dict[str, tuple[str, str]] = {
    "data": ("{app} data", "Shared data files for {app}."),
    "dev": ("{app} development files", "Development files for {app}."),
    "doc": ("{app} documentation", "Documentation for {app}."),
    "runtime": ("{app} runtime libraries", "Shared libraries for {app}."),
}


def _pkg_descriptions(
    app_name: str,
    bucket_name: str,
    is_primary: bool,
    meta: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Return default short and long descriptions for a package."""
    if is_primary:
        short = app_name
        if meta:
            if meta.get("description_short"):
                short = meta["description_short"].strip()
            elif meta.get("description"):
                short = meta["description"].strip()
            if not short:
                short = app_name

        if meta and meta.get("description_long"):
            long_ = meta["description_long"]
        else:
            long_ = f"Runtime package for {app_name}."
        return short, long_

    if bucket_name in _BUCKET_DESCRIPTIONS:
        short_tmpl, long_tmpl = _BUCKET_DESCRIPTIONS[bucket_name]
        return short_tmpl.format(app=app_name), long_tmpl.format(app=app_name)

    short = f"{app_name} {bucket_name}"
    long_ = f"{bucket_name.capitalize()} package for {app_name}."
    return short, long_
