"""Generate a minimal debian/ skeleton from a package-plan.json."""

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from debcraft.deps import infer_dependencies
from debcraft.utils.fs import ensure_dir, write_json
from debcraft.utils.log import info

_PLAN_FILE = "package-plan.json"
_RESULT_FILE = "generate-result.json"

# Buckets that warrant ${shlibs:Depends} in the control file.
_SHLIBS_BUCKETS = {"runtime", "bin", "plugins", "other"}

# Buckets that may carry inferred runtime dependencies.
_RUNTIME_BUCKETS = {"runtime", "bin", "plugins", "other"}

_MAINTAINER = "Joseph Wiley <flux.abyss@proton.me>"
_BUILD_DEPENDS = "debhelper-compat (= 13), meson, ninja-build, pkgconf"


def _orthos_dir(repo_path: Path) -> Path:
    """Mirror the layout used by all earlier steps."""
    base = Path.cwd() / ".orthos"
    return base / repo_path.name


def _load_plan(plan_file: Path) -> dict[str, Any]:
    """Read package-plan.json; raise FileNotFoundError if absent."""
    if not plan_file.exists():
        raise FileNotFoundError(f"package plan not found: {plan_file}\n"
                                f"Run 'orthos-packager classify <repo>' first.")
    data: dict[str, Any] = json.loads(plan_file.read_text(encoding="utf-8"))
    return data


def _non_empty_buckets(
        package_buckets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only buckets that contain at least one file."""
    return [b for b in package_buckets if b["file_count"] > 0]


def _binary_pkg_name(repo_name: str, bucket_name: str) -> str:
    # repo directory names often use underscores; Debian prefers hyphens.
    safe = repo_name.replace("_", "-")
    return f"{safe}-{bucket_name}"


def _gen_control(
    repo_name: str,
    non_empty: list[dict[str, Any]],
    inferred_deps: list[str],
) -> str:
    """Return the full text of debian/control."""
    src_name = repo_name.replace("_", "-")
    lines: list[str] = [
        f"Source: {src_name}",
        "Section: misc",
        "Priority: optional",
        f"Maintainer: {_MAINTAINER}",
        f"Build-Depends: {_BUILD_DEPENDS}",
        "Standards-Version: 4.6.2",
        "",
    ]

    for bucket in non_empty:
        pkg = _binary_pkg_name(repo_name, bucket["name"])
        depends = ["${misc:Depends}"]
        if bucket["name"] in _SHLIBS_BUCKETS:
            depends.append("${shlibs:Depends}")
        if bucket["name"] in _RUNTIME_BUCKETS:
            depends.extend(inferred_deps)
        lines += [
            f"Package: {pkg}",
            "Architecture: any",
            f"Depends: {', '.join(depends)}",
            f"Description: {src_name} {bucket['name']} files",
            f" Auto-generated {bucket['name']} package for {src_name}.",
            "",
        ]

    return "\n".join(lines)


def _gen_rules() -> str:
    """Return the full text of debian/rules."""
    return textwrap.dedent("""\
        #!/usr/bin/make -f

        %:
        \tdh $@ --buildsystem=meson
    """)


def _now_rfc2822() -> str:
    """Return current UTC time formatted for Debian changelog."""
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")


def _gen_changelog(repo_name: str) -> str:
    """Return the full text of debian/changelog."""
    src_name = repo_name.replace("_", "-")
    now = _now_rfc2822()

    return textwrap.dedent(f"""\
        {src_name} (0.1.0-1) unstable; urgency=medium

          * Initial release.

         -- {_MAINTAINER}  {now}
    """)


def _gen_source_format() -> str:
    """Return the full text of debian/source/format."""
    return "3.0 (native)\n"


def _gen_install(files: list[str]) -> str:
    """Return the text of a .install file: one path per line."""
    return "\n".join(files) + "\n" if files else ""


# pylint: disable=too-many-locals
def generate(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Generate a debian/ skeleton from the package plan for *meta*.
    Returns (exit_code, result_dict).
    """
    repo = Path(meta["repo_path"])
    repo_name = repo.name
    orthos = _orthos_dir(repo)
    plan_file = orthos / _PLAN_FILE

    plan = _load_plan(plan_file)  # raises FileNotFoundError if missing

    non_empty = _non_empty_buckets(plan["package_buckets"])
    debian_dir = orthos / "debian"
    source_dir = debian_dir / "source"

    for d in (debian_dir, source_dir):
        ensure_dir(d)

    generated: list[str] = []

    def write_text(rel: Path, content: str) -> None:
        rel.write_text(content, encoding="utf-8")
        generated.append(str(rel))

    stage_dir = orthos / "stage"
    dep_report = infer_dependencies(
        repo,
        stage_dir=stage_dir if stage_dir.exists() else None,
    )
    inferred_deps = dep_report.sorted_depends()
    dep_summary = ", ".join(inferred_deps) if inferred_deps else "(none)"
    info(f"inferred depends: {dep_summary}")
    for pkg, pkg_reasons in dep_report.sorted_reasons():
        info(f"  inferred reason: {pkg} <- {'; '.join(pkg_reasons)}")

    write_text(debian_dir / "control",
               _gen_control(repo_name, non_empty, inferred_deps))

    rules_path = debian_dir / "rules"
    write_text(rules_path, _gen_rules())
    rules_path.chmod(0o755)

    write_text(debian_dir / "changelog", _gen_changelog(repo_name))
    write_text(source_dir / "format", _gen_source_format())

    # One .install file per non-empty bucket.
    for bucket in non_empty:
        pkg_name = _binary_pkg_name(repo_name, bucket["name"])
        install_name = f"{pkg_name}.install"
        write_text(debian_dir / install_name, _gen_install(bucket["files"]))

    binary_packages = [
        _binary_pkg_name(repo_name, b["name"]) for b in non_empty
    ]

    result: dict[str, Any] = {
        "binary_packages": binary_packages,
        "debian_dir": str(debian_dir),
        "generated_files": generated,
        "plan_file": str(plan_file),
        "repo_path": str(repo),
    }

    write_json(orthos / _RESULT_FILE, result)
    return 0, result
