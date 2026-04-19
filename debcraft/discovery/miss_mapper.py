"""Map a DepMiss to a candidate Debian package name.

Resolution is deterministic. No hardcoded package maps are used for
pkg-config-miss or library-miss resolution beyond the curated seed maps.

Resolution order per miss type:

  pkg-config-miss:
    1. BODHI_BUILD_DEP_MAP (curated)
    2. runner.pkgconfig_file_search — apt-file search for <name>.pc in chroot
       (chroot mode only; installs apt-file and fetches Contents metadata on
       first use; subsequent calls use the cached database)
    3. runner.pkg_query_exists on lib<name>-dev then apt-cache search fallback

  tool-miss:
    1. TOOL_DEP_MAP (curated)
    2. runner.pkg_query_exists on <name> then lib<name>-dev

  header-miss:
    1. HEADER_DEP_MAP (curated)
    2. runner.dpkg_search_path (queries inside runner's environment)
    3. _apt_file_search_host — host mode only; not used in chroot mode
       (chroot isolation: querying the host apt-file database would reflect
       host package metadata, not the chroot's apt sources)

  library-miss:
    1. BODHI_BUILD_DEP_MAP (curated)
    2. runner.pkg_query_exists on lib<name>-dev then apt-cache search fallback
    3. runner.pkgconfig_file_search — apt-file search for <name>.pc in chroot
       (chroot mode only; many libraries ship a same-named .pc file)

All returned package names are normalised (lowercased, stripped).

Runner awareness:
  When a runner is provided, fallback package queries run inside the runner's
  own environment so chroot mode does not consult host package metadata.
  When runner is None, host-side tools are used directly.
  pkgconfig_file_search is only invoked when a runner is present; HostRunner
  returns None from that method (host header resolution uses _apt_file_search_host).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from debcraft.build_deps import BODHI_BUILD_DEP_MAP, _apt_cache_policy, _apt_search_dev
from debcraft.discovery.miss_classifier import DepMiss

if TYPE_CHECKING:
    from debcraft.discovery.runner import RunnerProtocol



# ---------------------------------------------------------------------------
# Curated tool name → Debian package map
# ---------------------------------------------------------------------------
# Keys are the tool/program names as Meson reports them (lowercase).
# A name present here causes the classifier to route "found: NO" dependency
# lines to tool-miss instead of pkg-config-miss.

TOOL_DEP_MAP: dict[str, str] = {
    # Wayland
    "wayland-scanner": "libwayland-bin",
    # GLib / GObject toolchain
    "glib-compile-resources": "libglib2.0-dev",
    "glib-compile-schemas": "libglib2.0-dev",
    "gdbus-codegen": "libglib2.0-dev",
    "glib-mkenums": "libglib2.0-dev",
    "gobject-introspection": "gobject-introspection",
    "g-ir-scanner": "gobject-introspection",
    "g-ir-compiler": "gobject-introspection",
    # Vala
    "vala": "valac",
    "vapigen": "valac",
    "valac": "valac",
    # Internationalization
    "msgfmt": "gettext",
    "msgmerge": "gettext",
    "xgettext": "gettext",
    "intltool-update": "intltool",
    "intltool-extract": "intltool",
    "intltool-merge": "intltool",
    # Core build tools
    "cmake": "cmake",
    "ninja": "ninja-build",
    "meson": "meson",
    "pkg-config": "pkg-config",
    "pkgconf": "pkgconf",
    # Languages / interpreters
    "python3": "python3",
    "python": "python3",
    "perl": "perl",
    # Parser generators
    "flex": "flex",
    "bison": "bison",
    "gperf": "gperf",
    # Documentation tools
    "asciidoc": "asciidoc",
    "asciidoctor": "ruby-asciidoctor",
    "xsltproc": "xsltproc",
    "doxygen": "doxygen",
    "gi-docgen": "gi-docgen",
    "sphinx-build": "python3-sphinx",
    "gtkdoc-scan": "gtk-doc-tools",
    "gtkdoc-mkdb": "gtk-doc-tools",
    "gtkdoc-mktmpl": "gtk-doc-tools",
    "gtkdoc-mkhtml": "gtk-doc-tools",
    # Desktop / AppStream
    "desktop-file-validate": "desktop-file-utils",
    "update-desktop-database": "desktop-file-utils",
    "appstream-util": "appstream-utils",
    "appstreamcli": "appstream",
    # XML / transform
    "xmllint": "libxml2-utils",
    # Man page generation
    "rst2man": "python3-docutils",
    "rst2man.py": "python3-docutils",
    # Compiler tools
    "llvm-config": "llvm-dev",
    "llvm": "llvm-dev",
    # Protocol / serialization compilers
    "protoc": "protobuf-compiler",
    "flatc": "flatbuffers-compiler",
    "capnp": "capnproto",
    "thrift": "thrift-compiler",
}


# ---------------------------------------------------------------------------
# Curated header path → Debian package map
# ---------------------------------------------------------------------------
# Keys are the header name as it appears in the cc.has_header() probe output
# (may be a basename or a relative path like "glib/glib.h").

HEADER_DEP_MAP: dict[str, str] = {
    # GLib
    "glib.h": "libglib2.0-dev",
    "glib/glib.h": "libglib2.0-dev",
    "glib-object.h": "libglib2.0-dev",
    # GTK
    "gtk/gtk.h": "libgtk-3-dev",
    "gtk4/gtk/gtk.h": "libgtk-4-dev",
    # Cairo
    "cairo.h": "libcairo2-dev",
    "cairo/cairo.h": "libcairo2-dev",
    # Pango
    "pango/pango.h": "libpango1.0-dev",
    # GDK-Pixbuf
    "gdk-pixbuf/gdk-pixbuf.h": "libgdk-pixbuf-2.0-dev",
    # OpenSSL
    "openssl/ssl.h": "libssl-dev",
    "openssl/evp.h": "libssl-dev",
    "openssl/err.h": "libssl-dev",
    # Compression
    "zlib.h": "zlib1g-dev",
    # Image
    "png.h": "libpng-dev",
    "jpeglib.h": "libjpeg-dev",
    "webp/encode.h": "libwebp-dev",
    # Fonts
    "ft2build.h": "libfreetype6-dev",
    "freetype/freetype.h": "libfreetype6-dev",
    "fontconfig/fontconfig.h": "libfontconfig1-dev",
    # X11
    "X11/Xlib.h": "libx11-dev",
    "X11/extensions/XShm.h": "libxext-dev",
    "X11/extensions/Xrender.h": "libxrender-dev",
    "X11/extensions/XInput2.h": "libxi-dev",
    # XCB
    "xcb/xcb.h": "libxcb1-dev",
    "xcb/xcb_renderutil.h": "libxcb-render-util0-dev",
    # Wayland
    "wayland-client.h": "libwayland-dev",
    "wayland-server.h": "libwayland-dev",
    "xkbcommon/xkbcommon.h": "libxkbcommon-dev",
    # Audio
    "alsa/asoundlib.h": "libasound2-dev",
    "pulse/pulseaudio.h": "libpulse-dev",
    "pipewire/pipewire.h": "libpipewire-0.3-dev",
    # GL / GPU
    "GL/gl.h": "libgl-dev",
    "GL/glx.h": "libgl-dev",
    "EGL/egl.h": "libegl-dev",
    "GLES2/gl2.h": "libgles2-mesa-dev",
    "gbm.h": "libgbm-dev",
    # DRM / input
    "libdrm/drm.h": "libdrm-dev",
    "libinput.h": "libinput-dev",
    "libudev.h": "libudev-dev",
    # Database
    "sqlite3.h": "libsqlite3-dev",
    # Parsing / serialization
    "expat.h": "libexpat1-dev",
    "libxml/tree.h": "libxml2-dev",
    "curl/curl.h": "libcurl4-openssl-dev",
    "ffi.h": "libffi-dev",
    "pcre.h": "libpcre3-dev",
    "pcre2.h": "libpcre2-dev",
    "json-c/json.h": "libjson-c-dev",
    "jansson.h": "libjansson-dev",
    "yaml.h": "libyaml-dev",
    # Security / PAM
    "security/pam_appl.h": "libpam0g-dev",
    # System
    "systemd/sd-bus.h": "libsystemd-dev",
}


# ---------------------------------------------------------------------------
# Helper: apt-file search (host mode only)
# ---------------------------------------------------------------------------


def _apt_file_search_host(header_name: str) -> str | None:
    """Search for a package providing *header_name* using apt-file.

    Host mode only. In isolated mode this function is NOT called — querying
    the host apt-file database would break chroot isolation.
    Returns None if apt-file is not installed, produces no results, or
    times out. apt-file must have been updated for results to be fresh.
    Deliberately skips -dbg and -dbgsym packages.
    """
    if not shutil.which("apt-file"):
        return None
    try:
        result = subprocess.run(
            ["apt-file", "search", "--regexp", f"/{header_name}$"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    for line in result.stdout.splitlines():
        # Output format: "package: /path/to/header"
        parts = line.split(":", 1)
        if len(parts) == 2:
            pkg = parts[0].strip()
            if pkg and "-dbg" not in pkg and "-dbgsym" not in pkg:
                return pkg.lower()
    return None



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def tool_dep_names() -> frozenset[str]:
    """Return the set of tool names in TOOL_DEP_MAP.

    Passed to classify_misses() so the classifier can route
    ``found: NO`` dependency lines to tool-miss deterministically.
    """
    return frozenset(TOOL_DEP_MAP.keys())


def map_miss_to_package(
    miss: DepMiss,
    runner: "RunnerProtocol | None" = None,
) -> str | None:
    """Resolve *miss* to a candidate Debian package name.

    When *runner* is provided, fallback package queries (apt-cache, dpkg -S)
    run inside the runner's own environment so chroot mode does not consult
    host package metadata.

    Returns a normalised (lowercased, stripped) package name, or None if
    no candidate can be determined via any resolution step.
    """
    from debcraft.utils.log import info  # local import to avoid circularity

    name = miss.name.strip().lower()
    in_chroot = runner is not None and runner.mode == "chroot"

    # ------------------------------------------------------------------
    # Helpers that route through the runner when available
    # ------------------------------------------------------------------

    def _pkg_exists(pkg: str) -> bool:
        if runner is not None:
            return runner.pkg_query_exists(pkg)
        exists, _ = _apt_cache_policy(pkg)
        return exists

    def _dev_search(meson_name: str) -> str | None:
        """Find a -dev package for *meson_name* via the runner's environment."""
        if runner is not None:
            return runner.apt_search_dev(meson_name)
        return _apt_search_dev(meson_name)

    def _dpkg_search(pattern: str) -> str | None:
        if runner is not None:
            return runner.dpkg_search_path(pattern)
        # Host fallback (legacy path, no runner).
        try:
            result = subprocess.run(
                ["dpkg", "-S", f"*/{pattern}"],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        line = result.stdout.strip().splitlines()[0]
        if ":" in line:
            pkg = line.split(":")[0].strip()
            return pkg.lower() if pkg else None
        return None

    # ------------------------------------------------------------------
    # tool-miss
    # ------------------------------------------------------------------
    if miss.miss_type == "tool-miss":
        # 1. Curated tool map.
        pkg = TOOL_DEP_MAP.get(name)
        if pkg:
            return pkg.strip().lower()

        # 2. pkg_query_exists: try <name> then lib<name>-dev.
        for attempt in (name, f"lib{name}-dev"):
            if _pkg_exists(attempt):
                return attempt.lower()

        return None

    # ------------------------------------------------------------------
    # pkg-config-miss
    # ------------------------------------------------------------------
    if miss.miss_type == "pkg-config-miss":
        # 1. Curated Meson/pkg-config map.
        pkg = BODHI_BUILD_DEP_MAP.get(name)
        if pkg:
            return pkg.strip().lower()

        # 2. apt-file search for <name>.pc inside the chroot.
        #    This is the authoritative resolver: it finds the exact package
        #    that ships the .pc file, derived from distro Contents metadata.
        #    Only meaningful in chroot mode (HostRunner returns None here).
        if runner is not None:
            pkg = runner.pkgconfig_file_search(name)
            if pkg:
                info(f"miss_mapper: pkgconfig-file-search resolved {name!r} -> {pkg}")
                return pkg.strip().lower()

        # 3. Runner-aware dev-package search (name pattern heuristic).
        pkg = _dev_search(name)
        if pkg:
            return pkg.strip().lower()

        return None

    # ------------------------------------------------------------------
    # header-miss  (configure-time probe only)
    # ------------------------------------------------------------------
    if miss.miss_type == "header-miss":
        basename = Path(miss.name).name.lower()
        # 1. Curated header map — try full path first, then basename.
        pkg = HEADER_DEP_MAP.get(miss.name) or HEADER_DEP_MAP.get(basename)
        if pkg:
            return pkg.strip().lower()

        # 2. dpkg_search_path — queries inside chroot in isolated mode,
        #    queries host in host mode.
        pkg = _dpkg_search(miss.name) or _dpkg_search(basename)
        if pkg:
            return pkg.strip().lower()

        # 3. _apt_file_search_host: host mode only.
        #    In chroot mode, querying the host apt-file database would reflect
        #    host package metadata rather than the chroot's apt sources, breaking
        #    isolation. Header resolution inside a chroot relies on steps 1 and 2.
        if in_chroot:
            info(
                f"miss_mapper: skipping host apt-file for header '{basename}' "
                "(chroot mode — host apt-file database would break isolation)"
            )
        else:
            pkg = _apt_file_search_host(basename)
            if pkg:
                return pkg.strip().lower()

        return None

    # ------------------------------------------------------------------
    # library-miss  (configure-time probe only)
    # ------------------------------------------------------------------
    if miss.miss_type == "library-miss":
        # 1. Curated map.
        pkg = BODHI_BUILD_DEP_MAP.get(name)
        if pkg:
            return pkg.strip().lower()

        # 2. Runner-aware dev-package search.
        pkg = _dev_search(name)
        if pkg:
            return pkg.strip().lower()

        # 3. apt-file search for <name>.pc — many libraries ship a same-named
        #    pkg-config file; this catches cases where the library name and the
        #    .pc name match but the package name does not follow the lib<x>-dev
        #    pattern (e.g. versioned names like lua51).
        if runner is not None:
            pkg = runner.pkgconfig_file_search(name)
            if pkg:
                info(f"miss_mapper: pkgconfig-file-search (library) resolved {name!r} -> {pkg}")
                return pkg.strip().lower()

        return None

    return None
