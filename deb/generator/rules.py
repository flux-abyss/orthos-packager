"""Render debian/rules for orthos-generated packages."""

import textwrap


def _gen_meson_configure_override(meson_options: dict[str, str]) -> str:
    """Return an override_dh_auto_configure stanza for debian/rules.

    Emits '-Dkey=value' flags in sorted key order for determinism.
    Returns empty string when meson_options is empty.
    """
    if not meson_options:
        return ""
    flags = " ".join(
        f"-D{k}={v}" for k, v in sorted(meson_options.items())
    )
    return textwrap.dedent(f"""\
        override_dh_auto_configure:
        \tdh_auto_configure -- {flags}
    """)


def _gen_rules(
    rules_overrides: str = "",
    build_backend: str = "meson",
    project_name: str = "",
) -> str:
    """Return debian/rules with optional override content appended.

    Support pybuild-based rules for python-pyproject packages; meson/generic
    rules with shlibdeps overrides otherwise.
    """
    if build_backend == "python-pyproject":
        pybuild_name = project_name or "project"
        base = textwrap.dedent(f"""\
            #!/usr/bin/make -f

            export PYBUILD_NAME={pybuild_name}
            export PYBUILD_SYSTEM=pyproject

            %:
            \tdh $@ --with python3 --buildsystem=pybuild
        """)
        extra = rules_overrides.strip()
        if extra:
            base += "\n" + extra + "\n"
        return base

    # The shlibdeps override is unconditional: it is harmless when building
    # against genuine Debian libraries and essential when building on a host
    # that has non-Debian libraries installed.
    _SHLIBDEPS_OVERRIDE = textwrap.dedent("""\
        override_dh_shlibdeps:
        \tdh_shlibdeps -- --ignore-missing-info
    """)
    base = textwrap.dedent("""\
        #!/usr/bin/make -f

        %:
        \tdh $@
    """)
    result = base + "\n" + _SHLIBDEPS_OVERRIDE
    extra = rules_overrides.strip()
    if extra:
        result += "\n" + extra + "\n"
    return result
