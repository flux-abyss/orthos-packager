"""Dependency inference for Python, GI, and CLI usage."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

PYTHON_IMPORT_MAP: dict[str, str] = {
    "gi": "python3-gi",
    "nltk": "python3-nltk",
    "Xlib": "python3-xlib",
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
}

CLI_COMMAND_MAP: dict[str, str] = {
    "xclip": "xclip",
}


@dataclass
class DependencyReport:
    """Holds inferred dependencies and the reasons they were added."""

    depends: set[str] = field(default_factory=set)
    python_imports: set[str] = field(default_factory=set)
    gi_namespaces: set[str] = field(default_factory=set)
    cli_commands: set[str] = field(default_factory=set)
    reasons: dict[str, list[str]] = field(default_factory=dict)

    def sorted_depends(self) -> list[str]:
        """Return inferred dependencies in sorted order."""
        return sorted(self.depends)

    def sorted_reasons(self) -> list[tuple[str, list[str]]]:
        """Return (package, reasons) pairs in alphabetical package order."""
        return [(pkg, sorted(set(self.reasons.get(pkg, []))))
                for pkg in sorted(self.depends)]


def infer_dependencies(repo: str | Path) -> DependencyReport:
    """Scan a repository tree and infer runtime dependencies."""
    root = Path(repo)
    report = DependencyReport()

    for py_file in root.rglob("*.py"):
        _scan_python_file(py_file, report)

    return report


def _record_reason(report: DependencyReport, pkg: str, reason: str) -> None:
    """Append *reason* to the reasons list for *pkg*, avoiding duplicates."""
    if pkg not in report.reasons:
        report.reasons[pkg] = []
    if reason not in report.reasons[pkg]:
        report.reasons[pkg].append(reason)


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

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                report.python_imports.add(top)
                pkg = PYTHON_IMPORT_MAP.get(top)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report, pkg, f"import {top}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                report.python_imports.add(top)
                pkg = PYTHON_IMPORT_MAP.get(top)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report, pkg, f"import {top}")

        elif _is_gi_require_version_call(node):
            call = cast(ast.Call, node)
            namespace = _extract_gi_namespace(call)
            if namespace:
                report.gi_namespaces.add(namespace)
                reason = _gi_reason(call, namespace)
                for pkg in GI_NAMESPACE_MAP.get(namespace, []):
                    report.depends.add(pkg)
                    _record_reason(report, pkg, reason)

        elif _is_subprocess_command(node):
            call = cast(ast.Call, node)
            command = _extract_command_name(call)
            if command:
                report.cli_commands.add(command)
                pkg = CLI_COMMAND_MAP.get(command)
                if pkg:
                    report.depends.add(pkg)
                    _record_reason(report, pkg, f"subprocess {command}")


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


def _is_subprocess_command(node: ast.AST) -> bool:
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
