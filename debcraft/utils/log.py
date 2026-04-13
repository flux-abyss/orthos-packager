"""Thin wrappers around print so callers don't import sys everywhere."""

import sys


def info(msg: str) -> None:
    print(msg)


def error(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
