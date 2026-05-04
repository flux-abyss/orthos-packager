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


def _gen_rules(rules_overrides: str = "") -> str:
    """Return debian/rules with optional override content appended.

    Always includes override_dh_shlibdeps with --ignore-missing-info so that
    dpkg-shlibdeps does not emit fabricated Debian package names derived from
    host-local shlibs registrations (e.g. a custom EFL build that registers
    'libefl' in the host dpkg database).  Without this, dh_shlibdeps would
    write shlibs:Depends=libefl (>= X.Y.Z) into the substvars file, producing
    a Depends entry that does not exist on the target Debian system.

    --ignore-missing-info silently skips any library whose shlibs data is
    absent from the dpkg database rather than fabricating an entry.  When the
    package is built inside a proper Debian chroot (where target libraries are
    registered with correct Debian package names), those entries flow through
    correctly and the override has no negative effect.
    """
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
