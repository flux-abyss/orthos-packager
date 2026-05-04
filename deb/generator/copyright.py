"""Render DEP-5 debian/copyright for orthos-generated packages."""

import textwrap
from datetime import datetime, timezone
from typing import Any


# Maps SPDX-ish identifiers (lowercase) to (Debian label, common-licenses path or None).
_LICENSE_MAP: dict[str, tuple[str, str | None]] = {
    "gpl-2": ("GPL-2", "GPL-2"),
    "gpl-2+": ("GPL-2+", "GPL-2"),
    "gpl-3": ("GPL-3", "GPL-3"),
    "gpl-3+": ("GPL-3+", "GPL-3"),
    "lgpl-2": ("LGPL-2", "LGPL-2"),
    "lgpl-2+": ("LGPL-2+", "LGPL-2"),
    "lgpl-2.1": ("LGPL-2.1", "LGPL-2.1"),
    "lgpl-2.1+": ("LGPL-2.1+", "LGPL-2.1"),
    "lgpl-3": ("LGPL-3", "LGPL-3"),
    "lgpl-3+": ("LGPL-3+", "LGPL-3"),
    "mit": ("MIT", None),
    "isc": ("ISC", None),
    "apache-2.0": ("Apache-2.0", None),
    "bsd-2-clause": ("BSD-2-Clause", None),
    "bsd-3-clause": ("BSD-3-Clause", None),
}


def _resolve_license(meta: dict[str, Any]) -> str:
    """Return the normalized Debian license label for the upstream license.

    Normalizes common Meson/SPDX license identifiers before lookup.
    Falls back to 'unknown' for unrecognized values.
    """
    raw = (meta.get("license") or "").strip()

    # Conservative normalization: map common Meson/SPDX variants to the
    # lowercase keys used by _LICENSE_MAP.  Only simple, unambiguous tokens
    # are mapped; compound SPDX expressions (" OR ", " AND ") are left to
    # the fallback so we never silently misrepresent the license.
    _SPDX_ALIASES: dict[str, str] = {
        "BSD 2 clause":      "bsd-2-clause",
        "BSD-2-Clause":      "bsd-2-clause",
        "BSD 3 clause":      "bsd-3-clause",
        "BSD-3-Clause":      "bsd-3-clause",
        "GPL-2.0":           "gpl-2",
        "GPL-2.0-only":      "gpl-2",
        "GPL-2.0-or-later":  "gpl-2+",
        "GPL-3.0":           "gpl-3",
        "GPL-3.0-only":      "gpl-3",
        "GPL-3.0-or-later":  "gpl-3+",
        "LGPL-2.1":          "lgpl-2.1",
        "LGPL-2.1-only":     "lgpl-2.1",
        "LGPL-2.1-or-later": "lgpl-2.1+",
        "MIT":               "mit",
        "ISC":               "isc",
        "ISC License":       "isc",
        "Apache-2.0":        "apache-2.0",
    }
    normalized = _SPDX_ALIASES.get(raw, raw).lower()

    entry = _LICENSE_MAP.get(normalized)
    if entry:
        label, _common = entry
        return label
    return "unknown"


def _format_dep5_license_text(text: str) -> str:
    """Format a multi-line license body as DEP-5 continuation text.

    Each non-blank line is prefixed with a single space.
    Blank lines become " .".
    The result is suitable for embedding directly after a License: field.
    """
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped:
            out.append(f" {stripped}")
        else:
            out.append(" .")
    # Remove trailing " ." lines.
    while out and out[-1] == " .":
        out.pop()
    return "\n".join(out)


def _gen_copyright(app_name: str, maintainer: str, meta: dict[str, Any]) -> str:
    """Return a DEP-5 debian/copyright with a single Files: * stanza."""
    upstream_name = meta.get("upstream_name") or meta.get("project_name") or app_name
    upstream_contact = meta.get("upstream_contact") or "FIXME"
    source_url = meta.get("source_url") or "FIXME"
    year = datetime.now(timezone.utc).year
    license_label = _resolve_license(meta)

    # Use a real copyright notice when the probe found one; otherwise FIXME.
    upstream_copyright = (meta.get("upstream_copyright") or "").strip()
    files_copyright = upstream_copyright or f"{year} FIXME <fixme@example.com>"

    # Use the upstream license body when found; otherwise emit an explicit FIXME.
    upstream_license_text = (meta.get("upstream_license_text") or "").strip()
    if upstream_license_text:
        license_body = _format_dep5_license_text(upstream_license_text)
    else:
        license_body = " FIXME: upstream license text not found; human review required."

    header = textwrap.dedent(f"""\
        Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
        Upstream-Name: {upstream_name}
        Upstream-Contact: {upstream_contact}
        Source: {source_url}
    """)

    files_star = (
        f"Files: *\n"
        f"Copyright: {files_copyright}\n"
        f"License: {license_label}\n"
    )

    files_debian = (
        f"Files: debian/*\n"
        f"Copyright: {year} {maintainer}\n"
        f"License: {license_label}\n"
    )

    standalone_license = (
        f"License: {license_label}\n"
        f"{license_body}\n"
    )

    return header + "\n" + files_star + "\n" + files_debian + "\n" + standalone_license
