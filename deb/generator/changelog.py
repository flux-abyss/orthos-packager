"""Render debian/changelog for orthos-generated packages."""

import textwrap
from datetime import datetime, timezone

_DEBIAN_REVISION = "1"


def _now_rfc2822() -> str:
    """Return current UTC time formatted for Debian changelog."""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _gen_changelog(app_name: str, version: str, maintainer: str) -> str:
    """Return the full text of debian/changelog."""
    now = _now_rfc2822()
    deb_version = f"{version}-{_DEBIAN_REVISION}"
    return textwrap.dedent(f"""\
        {app_name} ({deb_version}) unstable; urgency=medium

          * Initial release.

         -- {maintainer}  {now}
    """)
