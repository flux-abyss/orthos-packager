"""Meson option parsing for orthos-packager CLI."""

import re
import sys

from deb.utils.log import error

_KEY_RE = re.compile(r'^[A-Za-z0-9_.\-]+$')


def parse_meson_options(raw: list[str] | None) -> dict[str, str]:
    """Validate and parse --meson-option KEY=VALUE entries.

    Each entry must contain exactly one '=', a non-empty key composed only of
    letters, digits, underscores, dashes, and dots, and a non-empty value.
    Raises SystemExit with a clear message on the first malformed entry.
    """
    result: dict[str, str] = {}
    for entry in (raw or []):
        if entry.count("=") != 1:
            error(f"--meson-option: expected KEY=VALUE, got {entry!r}")
            sys.exit(1)
        key, value = entry.split("=", 1)
        if not key:
            error(f"--meson-option: empty key in {entry!r}")
            sys.exit(1)
        if not value:
            error(f"--meson-option: empty value in {entry!r}")
            sys.exit(1)
        if not _KEY_RE.match(key):
            error(f"--meson-option: invalid key {key!r} (use letters/digits/_-.only)")
            sys.exit(1)
        result[key] = value
    return result
