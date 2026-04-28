"""Thin wrappers around print so callers don't import sys everywhere."""

import sys


def info(msg: str) -> None:
    """Print an informational message to stdout."""
    print(msg)


def error(msg: str) -> None:
    """Print an error message to stderr."""
    print(f"error: {msg}", file=sys.stderr)
