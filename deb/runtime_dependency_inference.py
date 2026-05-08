"""Infer runtime dependencies from source and staged artifact evidence."""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

PYTHON_IMPORT_MAP: dict[str, str] = {
    "gi": "python3-gi",
    "nltk": "python3-nltk",
    "Xlib": "python3-xlib",
    "requests": "python3-requests",
    "yaml": "python3-yaml",
    "PIL": "python3-pil",
    "dbus": "python3-dbus",
    "apt": "python3-apt",
}

GI_NAMESPACE_MAP: dict[str, list[str]] = {
    "Gtk": ["gir1.2-gtk-3.0"],
    "Gdk": ["gir1.2-gtk-3.0"],
    "GLib": ["gir1.2-glib-2.0"],
    "Gio": ["gir1.2-glib-2.0"],
    "Granite": ["gir1.2-granite-1.0", "libgranite6"],
    "Vte": ["gir1.2-vte-2.91"],
    "Notify": ["gir1.2-notify-0.7"],
    "Pango": ["gir1.2-pango-1.0"],
    "AppIndicator3": ["gir1.2-ayatanaappindicator3-0.1"],
}

CLI_COMMAND_MAP: dict[str, str] = {
    "xclip": "xclip",
    "debootstrap": "debootstrap",
    "apt-file": "apt-file",
    "apt-get": "apt",
    "apt-cache": "apt",
    "dpkg": "dpkg",
    "dpkg-deb": "dpkg",
    "dpkg-buildpackage": "dpkg-dev",
    "meson": "meson",
    "ninja": "ninja-build",
    "pkg-config": "pkgconf",
    "pkgconf": "pkgconf",
}

# Conservative map from PyPI Requires-Dist names to Debian package names.
# Only well-known, unambiguous mappings are included.
# False negatives are acceptable; false positives must be avoided.
REQUIRES_DIST_MAP: dict[str, str] = {
    "requests": "python3-requests",
    "pyyaml": "python3-yaml",
    "pillow": "python3-pil",
    "dbus-python": "python3-dbus",
    "python-apt": "python3-apt",
    "pygobject": "python3-gi",
    "tomli": "python3-tomli",
    "packaging": "python3-packaging",
    "setuptools": "python3-setuptools",
}


@dataclass
class DependencyReport:
    """Holds inferred dependencies and the reasons they were added."""

    depends: set[str] = field(default_factory=set)
    python_imports: set[str] = field(default_factory=set)
    gi_namespaces: set[str] = field(default_factory=set)
    cli_commands: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)
    # provenance[pkg] = source label: "python-import", "gi-namespace",
    # "subprocess", "elf-dynamic", "script-command", or "fallback".
    # "elf-dynamic" means visible dynamic linkage via ldd (not static).
    provenance: dict[str, str] = field(default_factory=dict)

    def sorted_depends(self) -> list[str]:
        """Return inferred dependencies in sorted order."""
        return sorted(self.depends)

    def sorted_reasons(self) -> list[tuple[str, list[str]]]:
        """Return (package, reasons) pairs in alphabetical package order."""
        return [(pkg, sorted(set(self.reasons.get(pkg, []))))
                for pkg in sorted(self.depends)]


def infer_dependencies(
    repo: str | Path,
    stage_dir: Path | None = None,
) -> DependencyReport:
    """Scan a repository tree and infer runtime dependencies.

    Args:
        repo: path to the source repository (used for Python/GI/CLI scanning).
        stage_dir: optional path to the staged install tree; when supplied,
            ELF binaries are located there and ldd is run against them.
    """
    root = Path(repo)
    report = DependencyReport()

    for py_file in root.rglob("*.py"):
        _scan_python_file(py_file, report)

    if stage_dir is not None:
        _scan_dist_info_metadata(stage_dir, report)
        _scan_elf_tree(stage_dir, report)

    return report


def _record_reason(report: DependencyReport,
                   pkg: str,
                   reason: str,
                   provenance: str = "inferred") -> None:
    """Append *reason* to the reasons list for *pkg*, avoiding duplicates."""
    if pkg not in report.reasons:
        report.reasons[pkg] = []
    if reason not in report.reasons[pkg]:
        report.reasons[pkg].append(reason)
    # First provenance label wins.
    if pkg not in report.provenance:
        report.provenance[pkg] = provenance


# pylint: disable=too-many-branches
def _scan_python_file(path: Path, report: DependencyReport) -> None:
    """Scan a Python file and record dependency signals."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return

    imported_from_subprocess = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for name in node.names:
                imported_from_subprocess.add(name.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                report.python_imports.add(top)
                pkg = PYTHON_IMPORT_MAP.get(top)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report,
                                   pkg,
                                   f"import {top}",
                                   provenance="python-import")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                report.python_imports.add(top)
                pkg = PYTHON_IMPORT_MAP.get(top)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report,
                                   pkg,
                                   f"import {top}",
                                   provenance="python-import")
                # Detect: from gi.repository import Gtk, Gio, ...
                # Extract each alias name as a GI namespace.
                if node.module in ("gi.repository", "gi") or top == "gi":
                    for alias in node.names:
                        ns = alias.name
                        if ns in GI_NAMESPACE_MAP:
                            report.gi_namespaces.add(ns)
                            reason = f"from gi.repository import {ns}"
                            for gi_pkg in GI_NAMESPACE_MAP[ns]:
                                report.depends.add(gi_pkg)
                                _record_reason(report,
                                               gi_pkg,
                                               reason,
                                               provenance="gi-namespace")

        elif _is_gi_require_version_call(node):
            call = cast(ast.Call, node)
            namespace = _extract_gi_namespace(call)
            if namespace:
                report.gi_namespaces.add(namespace)
                reason = _gi_reason(call, namespace)
                for pkg in GI_NAMESPACE_MAP.get(namespace, []):
                    report.depends.add(pkg)
                    _record_reason(report,
                                   pkg,
                                   reason,
                                   provenance="gi-namespace")

        elif _is_subprocess_command(node, imported_from_subprocess):
            call = cast(ast.Call, node)
            command = _extract_command_name(call)
            if command:
                report.cli_commands.add(command)
                pkg = CLI_COMMAND_MAP.get(command)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report,
                                   pkg,
                                   f"subprocess {command}",
                                   provenance="subprocess")


def _is_gi_require_version_call(node: ast.AST) -> bool:
    """Return True when *node* is a gi.require_version(...) call."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Attribute) and
            func.attr == "require_version" and
            isinstance(func.value, ast.Name) and func.value.id == "gi")


def _extract_gi_namespace(node: ast.Call) -> str | None:
    """Extract the GI namespace from a gi.require_version call."""
    if not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def _gi_reason(node: ast.Call, namespace: str) -> str:
    """Return a human-readable reason string for a gi.require_version call."""
    if len(node.args) >= 2:
        ver_node = node.args[1]
        if isinstance(ver_node, ast.Constant) and isinstance(
                ver_node.value, str):
            return f'gi.require_version("{namespace}", "{ver_node.value}")'
    return f"gi namespace {namespace}"


def _is_subprocess_command(node: ast.AST, imported_from_subprocess: set[str]) -> bool:
    """Return True when *node* is a subprocess command invocation we track."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in {
            "run",
            "Popen",
            "call",
            "check_call",
            "check_output",
    }:
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            return True
    elif isinstance(func, ast.Name) and func.id in imported_from_subprocess:
        return True
    return False


def _extract_command_name(node: ast.Call) -> str | None:
    """Extract the executable name from a subprocess call."""
    if not node.args:
        return None

    first = node.args[0]

    if isinstance(first, ast.List) and first.elts:
        elt0 = first.elts[0]
        if isinstance(elt0, ast.Constant) and isinstance(elt0.value, str):
            return elt0.value

    if isinstance(first, ast.Tuple) and first.elts:
        elt0 = first.elts[0]
        if isinstance(elt0, ast.Constant) and isinstance(elt0.value, str):
            return elt0.value

    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        parts = first.value.strip().split()
        if parts:
            return parts[0]

    return None


# ---------------------------------------------------------------------------
# Requires-Dist scanning from staged dist-info
# ---------------------------------------------------------------------------

def _scan_dist_info_metadata(stage_dir: Path, report: DependencyReport) -> None:
    """Scan staged *.dist-info/METADATA files for Requires-Dist entries.

    For each Requires-Dist line found, map the distribution name conservatively
    to a Debian package using REQUIRES_DIST_MAP.  Extras and version
    constraints are ignored — we only record the bare distribution name.
    Unknown names are silently skipped (false negatives preferred).
    """
    for metadata_file in stage_dir.rglob("*.dist-info/METADATA"):
        try:
            text = metadata_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("Requires-Dist:"):
                continue
            # Format: Requires-Dist: <name> [version] [; extras]
            # Strip prefix, split on first whitespace or semicolon.
            raw = line[len("Requires-Dist:"):].strip()
            # Drop extras marker and version constraint.
            bare = raw.split(";")[0].strip()
            bare = bare.split()[0].strip() if bare else ""
            if not bare:
                continue
            pkg = REQUIRES_DIST_MAP.get(bare.lower())
            if pkg:
                report.depends.add(pkg)
                _record_reason(report,
                               pkg,
                               f"Requires-Dist: {bare}",
                               provenance="requires-dist")


# ---------------------------------------------------------------------------
# ELF runtime dependency inference
# ---------------------------------------------------------------------------

_ELF_MAGIC = b"\x7fELF"

# Packages whose runtime presence is guaranteed by the build toolchain or
# libc/kernel interface and should not appear in explicit Depends.
# - libc6, libm: pulled transitively by almost every ELF binary
# - linux-vdso: kernel artefact, not a real package
# - libgcc-s1, libstdc++6: base compiler runtime, always present
# - libdl, libpthread, librt: merged into glibc on modern systems
_ELF_SKIP_PACKAGES = {
    "libc6",
    "libm6",
    "libgcc-s1",
    "libstdc++6",
    "libdl",
    "libpthread",
    "librt",
}


def _is_elf(path: Path) -> bool:
    """Return True when the file starts with the ELF magic bytes."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) == _ELF_MAGIC
    except OSError:
        return False


def _ldd_libs(elf: Path) -> list[str]:
    """Return absolute resolved paths of shared libraries from ldd output.

    Lines we care about look like:
        libfoo.so.1 => /usr/lib/x86_64-linux-gnu/libfoo.so.1 (0x...)
    Lines for vdso / not-found are skipped.
    """
    try:
        result = subprocess.run(
            ["ldd", str(elf)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        # Format: <soname> => <path> (addr)  - we want index 2
        if len(parts) >= 3 and parts[1] == "=>" and parts[2].startswith("/"):
            paths.append(parts[2])
    return paths


def _dpkg_owner(lib_path: str) -> str | None:
    """Return the Debian package owning *lib_path*, or None.

    Uses 'realpath' to resolve symlinks before querying 'dpkg -S',
    because /lib is a symlink to /usr/lib on modern systems and dpkg's
    index stores the canonical path.
    """
    try:
        real = str(Path(lib_path).resolve())
        result = subprocess.run(
            ["dpkg", "-S", real],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    line = result.stdout.strip().splitlines(
    )[0] if result.returncode == 0 else ""
    if ":" in line:
        return line.split(":")[0].strip() or None
    return None


def _scan_elf_tree(stage_dir: Path, report: DependencyReport) -> None:
    """Walk *stage_dir*, run ldd on every ELF file, map libs to packages.

    Only dynamic (shared-object) linkage visible in ldd output is recorded.
    Statically linked code produces no ldd output and therefore generates
    no elf-dynamic provenance entries - this is intentional.

    Packages already recorded for any other ELF in this tree are not
    re-added; reasons accumulate but the depends set stays deduplicated.
    """
    for candidate in stage_dir.rglob("*"):
        if not candidate.is_file() or not _is_elf(candidate):
            continue
        try:
            installed = "/" + str(candidate.relative_to(stage_dir))
        except ValueError:
            installed = str(candidate)

        for lib_path in _ldd_libs(candidate):
            pkg = _dpkg_owner(lib_path)
            if not pkg or pkg in _ELF_SKIP_PACKAGES:
                continue
            lib_name = Path(lib_path).name
            reason = f"elf-dynamic: {installed} -> {lib_name}"
            report.depends.add(pkg)
            _record_reason(report, pkg, reason, provenance="elf-dynamic")
