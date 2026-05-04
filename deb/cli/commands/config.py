"""config command handler."""


def cmd_config(action: str) -> int:
    """Manage orthos configuration."""
    from deb.config import (
        get_maintainer_identity_result,
        save_maintainer_config,
        validate_maintainer_name,
        validate_email,
        format_debian_identity,
        DEFAULT_MAINTAINER,
        get_user_config_path
    )

    if action == "show":
        res = get_maintainer_identity_result()
        if res["reason"] == "invalid-config":
            print("Warning: The config file is unreadable or contains invalid TOML.")
            print(f"Maintainer: {DEFAULT_MAINTAINER}")
            print("To set your maintainer identity, run:")
            print("  orthos-packager config init")
        elif res["is_default"]:
            print(f"Maintainer: {DEFAULT_MAINTAINER}")
            print("To set your maintainer identity, run:")
            print("  orthos-packager config init")
        else:
            print(f"Maintainer: {res['identity']}")
        return 0

    if action == "init":
        try:
            name = input("Maintainer Name: ").strip()
            email = input("Maintainer Email: ").strip()
        except EOFError:
            return 1

        if not validate_maintainer_name(name):
            print("Error: Name cannot be empty.")
            return 1

        if not validate_email(email):
            print("Error: Invalid email format.")
            return 1

        try:
            identity = format_debian_identity(name, email)
        except ValueError:
            return 1

        print(f"Rendered identity: {identity}")

        try:
            confirm = input("Save this identity? [y/N] ").strip().lower()
        except EOFError:
            return 1

        if confirm not in ("y", "yes"):
            print("Aborted.")
            return 1

        try:
            save_maintainer_config(name, email)
            print(f"Successfully saved to {get_user_config_path()}")
            return 0
        except OSError as e:
            print(f"Error saving config: {e}")
            return 1
        except ValueError as e:
            print(f"Error: {e}")
            return 1

    return 1
