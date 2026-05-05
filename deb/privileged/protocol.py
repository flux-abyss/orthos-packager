"""JSON protocol response helpers for orthos-priv."""

from __future__ import annotations

import json
import sys


def _ok(result: object = None) -> None:
    print(json.dumps({"ok": True, "result": result}), flush=True)

def _fail(message: str) -> None:
    print(json.dumps({"ok": False, "error": message}), flush=True)

def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
