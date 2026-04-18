"""Launcher layer for the orthos-priv privileged helper.

This module is the only place in the unprivileged core that names the
authorization backend. The client API calls invoke() without knowing which
backend is in use.

Helper resolution order:
  1. `orthos-priv` executable found via shutil.which()  — installed path
  2. helper.py's own filesystem path                    — development fallback

Authorization backend (swappable seam):
  _USE_PKEXEC = False  → sudo <helper_path> (transitional bridge, prompts user)
  _USE_PKEXEC = True   → pkexec <helper_path> (polkit-gated, intended model)

TRANSITIONAL NOTE:
  The sudo bridge is a development convenience only. It is not the endorsed
  deployment model. The intended path is:
    - install orthos-priv at a fixed system path
    - define a polkit action for each allowed operation
    - flip _USE_PKEXEC to True
    - remove any manual sudoers entries

  If a temporary sudoers entry is added for development, it must target the
  fixed executable path only:
      <user> ALL=(root) NOPASSWD: /usr/local/bin/orthos-priv
  Never use a broad python3 -m ... sudoers rule.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration — the swappable authorization seam
# ---------------------------------------------------------------------------

# Flip to True when polkit action and pkexec are in place.
# Until then, the sudo bridge is used with an interactive prompt.
_USE_PKEXEC: bool = False


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class PrivilegedHelperError(RuntimeError):
    """Raised when the privileged helper returns an error or fails to launch."""


# ---------------------------------------------------------------------------
# Helper path resolution
# ---------------------------------------------------------------------------

def _find_helper() -> str:
    """Return the absolute path to the orthos-priv helper executable.

    Resolution order:
      1. orthos-priv on PATH (installed via pip / system package)
      2. helper.py in the same package directory (development fallback)

    Raises PrivilegedHelperError if neither is found.
    """
    # 1. Installed executable (fixed path — suitable for sudoers / pkexec).
    installed = shutil.which("orthos-priv")
    if installed:
        return installed

    # 2. Development fallback: use the helper.py file path directly.
    #    This requires the file to be executable (`chmod +x helper.py`)
    #    and to have the correct shebang line.
    dev_path = Path(__file__).parent / "helper.py"
    if dev_path.exists():
        return str(dev_path)

    raise PrivilegedHelperError(
        "orthos-priv helper not found. "
        "Install the package (pip install -e .) so the orthos-priv entry "
        "point is available on PATH, or ensure helper.py is executable."
    )


# ---------------------------------------------------------------------------
# Core invocation
# ---------------------------------------------------------------------------

def invoke(operation: str, args: dict) -> dict:
    """Invoke the privileged helper for *operation* with *args*.

    *args* is serialized to JSON and passed via --args. The helper writes a
    single JSON line to stdout; stderr passes through to the caller's terminal.

    Returns the parsed result dict on success.
    Raises PrivilegedHelperError on launch failure or helper-reported error.
    """
    helper_path = _find_helper()
    args_json = json.dumps(args)

    if _USE_PKEXEC:
        # Intended model: pkexec delegates authorization to polkit.
        launcher_cmd = ["pkexec", helper_path, operation, "--args", args_json]
    else:
        # Transitional bridge: interactive sudo against the fixed executable.
        # TRANSITIONAL — not the endorsed long-term model.
        launcher_cmd = ["sudo", helper_path, operation, "--args", args_json]

    try:
        result = subprocess.run(
            launcher_cmd,
            stdout=subprocess.PIPE,
            stderr=None,   # helper's stderr passes through to the user's terminal
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PrivilegedHelperError(
            f"could not launch helper ({launcher_cmd[0]}): {exc}"
        ) from exc

    if result.returncode != 0:
        # Helper itself may have printed an error JSON line or plain text.
        raw = (result.stdout or "").strip()
        try:
            parsed = json.loads(raw)
            msg = parsed.get("error", raw)
        except json.JSONDecodeError:
            msg = raw or f"helper exited {result.returncode}"
        raise PrivilegedHelperError(
            f"orthos-priv {operation!r} failed: {msg}"
        )

    raw_stdout = (result.stdout or "").strip()
    if not raw_stdout:
        raise PrivilegedHelperError(
            f"orthos-priv {operation!r}: no output from helper"
        )

    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise PrivilegedHelperError(
            f"orthos-priv {operation!r}: could not parse helper output: {exc}"
        ) from exc

    if not parsed.get("ok"):
        raise PrivilegedHelperError(
            f"orthos-priv {operation!r}: {parsed.get('error', 'unknown error')}"
        )

    return parsed
