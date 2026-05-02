"""User configuration for Orthos packager."""

import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_MAINTAINER = "Unknown Maintainer <fixme@example.com>"


class ConfigError(Exception):
    """Raised when the configuration file is unreadable or corrupt."""
    pass


def get_user_config_path() -> Path:
    """Return the path to the user configuration file."""
    return Path("~/.config/orthos/orthos.toml").expanduser()


def load_user_config() -> dict[str, Any]:
    """Load the user configuration from TOML."""
    path = get_user_config_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Cannot read config: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Corrupt config: {exc}") from exc


def validate_maintainer_name(name: str) -> bool:
    """Validate that a maintainer name is non-empty and has no newlines."""
    name = name.strip()
    if not name:
        return False
    if "\n" in name or "\r" in name:
        return False
    return True


def validate_email(email: str) -> bool:
    """Validate email with a simple sane check."""
    email = email.strip()
    if not email:
        return False
    if re.search(r'\s', email):
        return False
    if "\n" in email or "\r" in email:
        return False
    if "<" in email or ">" in email:
        return False
    if email.count("@") != 1:
        return False
    local, domain = email.split("@", 1)
    if not local or not domain:
        return False
    if "." not in domain:
        return False
    return True


def format_debian_identity(name: str, email: str) -> str:
    """Format name and email as a Debian identity string."""
    name = name.strip()
    email = email.strip()
    if not validate_maintainer_name(name) or not validate_email(email):
        raise ValueError("Invalid maintainer name or email")
    return f"{name} <{email}>"


def _default_identity(reason: str) -> dict[str, Any]:
    return {
        "identity": DEFAULT_MAINTAINER,
        "is_default": True,
        "reason": reason,
    }


def _configured_identity(identity: str) -> dict[str, Any]:
    return {
        "identity": identity,
        "is_default": False,
        "reason": "configured",
    }


def get_maintainer_identity_result() -> dict[str, Any]:
    """Return identity status including fallback reasons."""
    try:
        config = load_user_config()
    except ConfigError:
        return _default_identity("invalid-config")

    if not config:
        return _default_identity("missing")

    m = config.get("maintainer")
    if not isinstance(m, dict):
        return _default_identity("missing-maintainer")

    name = str(m.get("name", ""))
    email = str(m.get("email", ""))

    try:
        identity = format_debian_identity(name, email)
        return _configured_identity(identity)
    except ValueError:
        return _default_identity("invalid-maintainer")


def get_maintainer_identity() -> str:
    """Return the configured maintainer identity or the fallback default."""
    return get_maintainer_identity_result()["identity"]


def _escape_toml_string(s: str) -> str:
    """Escape backslash and double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_key(line: str) -> str | None:
    """Return the stripped key before '=' for a TOML key-value line.
    
    Ignores blank lines and comments.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def save_maintainer_config(name: str, email: str, config_path: Path | None = None) -> None:
    """Save maintainer config to TOML atomically, preserving other keys."""
    if not validate_maintainer_name(name) or not validate_email(email):
        raise ValueError("Invalid name or email")

    if config_path is None:
        config_path = get_user_config_path()

    config_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    if config_path.exists():
        try:
            lines = config_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    new_lines = []
    current_section = None

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = stripped[1:-1].strip()

        if current_section == "maintainer":
            key = _toml_key(line)
            if key == "name" or key == "email":
                continue

        new_lines.append(line)

    has_maintainer = False
    for line in new_lines:
        if line.strip() == "[maintainer]":
            has_maintainer = True
            break

    name_escaped = _escape_toml_string(name.strip())
    email_escaped = _escape_toml_string(email.strip())

    final_lines = []
    if not has_maintainer:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        final_lines.extend(new_lines)
        final_lines.append("[maintainer]")
        final_lines.append(f'name = "{name_escaped}"')
        final_lines.append(f'email = "{email_escaped}"')
    else:
        for line in new_lines:
            final_lines.append(line)
            if line.strip() == "[maintainer]":
                final_lines.append(f'name = "{name_escaped}"')
                final_lines.append(f'email = "{email_escaped}"')

    fd, temp_path = tempfile.mkstemp(dir=config_path.parent, prefix="orthos-", suffix=".toml")
    try:
        with os.fdopen(fd, 'w', encoding="utf-8") as f:
            f.write("\n".join(final_lines) + "\n")
        os.replace(temp_path, config_path)
    except OSError:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
