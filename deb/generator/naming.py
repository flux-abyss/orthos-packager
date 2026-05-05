"""Package naming and bucket collapse helpers for orthos generator."""

from typing import Any

# The bucket that carries the main executable.
_BIN_BUCKET = "bin"


def _primary_bucket_name(non_empty: list[dict[str, Any]]) -> str | None:
    """Return the name of the primary (executable-bearing) bucket.

    The bin bucket is always primary when it has content.  If there is no
    bin bucket, the first non-empty bucket in canonical order is used.
    """
    for b in non_empty:
        if b["name"] == _BIN_BUCKET:
            return _BIN_BUCKET
    return non_empty[0]["name"] if non_empty else None


def _should_collapse(non_empty: list[dict[str, Any]]) -> bool:
    """Return True when all non-empty buckets can be merged into one package.

    Collapse when the only non-empty buckets are the executable-bearing
    bucket and/or the data bucket - no shared libs, dev headers, doc,
    or other content that would justify a separate package.
    """
    names = {b["name"] for b in non_empty}
    return names <= {_BIN_BUCKET, "data"}


def _pkg_name(app_name: str, bucket_name: str, primary: str | None) -> str:
    """Return the Debian binary package name for *bucket_name*.

    The primary bucket (executable-bearing, or the sole non-empty bucket)
    is named after the application with no suffix.  All secondary buckets
    receive a hyphen-separated suffix: <app>-data, <app>-dev, etc.
    """
    if bucket_name == primary:
        return app_name
    return f"{app_name}-{bucket_name}"


def _merged_files(non_empty: list[dict[str, Any]]) -> list[str]:
    """Return a sorted, combined file list across all non-empty buckets."""
    all_files: list[str] = []
    for b in non_empty:
        all_files.extend(b["files"])
    return sorted(all_files)
