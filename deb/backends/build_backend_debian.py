"""Debian package build backend: build from repo debian/ and retain artifacts."""

import json
import shutil
from pathlib import Path
from typing import Any

from deb.backends.build_backend_meson import _clean_env
from deb.debian_clean import clean_debian_build_artifacts
from deb.paths import orthos_dir
from deb.resolution.debian import validate_built_debs
from deb.resolution.oracle import make_oracle
from deb.utils.fs import ensure_dir, write_json
from deb.utils.log import error, info
from deb.utils.shell import run_logged

_RESULT_FILE = "build-result.json"
_ARTIFACT_GLOBS = ("*.deb", "*.changes", "*.buildinfo")

# Transient directories removed from the orthos workspace after a successful build.
_TRANSIENT_DIRS = ("stage",)


def _parse_changes_files(changes_path: Path) -> list[Path]:
    """Return the list of file paths declared in a .changes Files: stanza.

    Lines in the stanza have the form:
      <md5> <size> <section> <priority> <filename>
    We return each filename resolved relative to changes_path.parent.
    Stops at the next RFC-822 field (a line that is not indented and
    contains a colon) or EOF.
    """
    parent = changes_path.parent
    files: list[Path] = []
    in_files = False
    try:
        text = changes_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if line.startswith("Files:"):
            in_files = True
            continue
        if in_files:
            if line.startswith(" ") or line.startswith("\t"):
                parts = line.split()
                if len(parts) == 5:  # md5 size section priority filename
                    candidate = parent / parts[4]
                    if candidate.exists():
                        files.append(candidate)
            else:
                break  # next RFC-822 field - stanza is over
    return files


def _find_changes_file(parent: Path, source_name: str) -> Path | None:
    """Return the .changes file produced for *source_name* in *parent*, or None.

    dpkg-buildpackage names it <source>_<version>_<arch>.changes.
    We match on the source-name prefix to avoid guessing the version.
    """
    prefix = source_name + "_"
    for p in sorted(parent.glob("*.changes")):
        if p.name.startswith(prefix):
            return p
    return None


def _collect_from_parent(repo: Path, source_name: str) -> list[Path]:
    """Return artifact paths in repo.parent that belong to *source_name*.

    Primary strategy: parse the .changes file written by dpkg-buildpackage,
    which is the authoritative manifest of every file produced for this build.
    The .changes file itself is included in the returned list.

    Fallback (no .changes found): glob all known artifact extensions and
    keep only those whose filename starts with the source package name.
    This prevents collecting unrelated packages that happen to live in the
    same directory.
    """
    parent = repo.parent
    changes = _find_changes_file(parent, source_name)
    if changes is not None:
        # Collect everything listed inside the .changes manifest.
        listed = _parse_changes_files(changes)
        # Always include the .changes file itself.
        all_files = sorted({changes, *listed})
        return all_files

    # Fallback: filter by source-name prefix.
    prefix = source_name + "_"
    found: list[Path] = []
    for pattern in _ARTIFACT_GLOBS:
        for p in parent.glob(pattern):
            if p.name.startswith(prefix):
                found.append(p)
    return sorted(found)


def _retain_artifacts(repo: Path, orthos: Path, source_name: str) -> list[str]:
    """Move build artifacts into orthos/artifacts and return their paths.

    Before collecting new artifacts, any existing file in orthos/artifacts
    whose name starts with "<source_name>_" is removed so that stale outputs
    from previous builds of the same package do not appear in the result.
    """
    artifacts_dir = orthos / "artifacts"
    ensure_dir(artifacts_dir)

    # Clear stale artifacts for this source package.
    prefix = source_name + "_"
    for stale in list(artifacts_dir.iterdir()):
        if stale.is_file() and stale.name.startswith(prefix):
            stale.unlink()

    retained: list[str] = []
    for src in _collect_from_parent(repo, source_name):
        dest = artifacts_dir / src.name
        if dest.exists():
            dest.unlink()
        shutil.move(str(src), dest)
        retained.append(str(dest.resolve()))
        info(f"artifact: {dest}")

    return sorted(retained)


def _cleanup_transient(orthos: Path) -> None:
    """Remove transient workspace directories after a successful build."""
    for name in _TRANSIENT_DIRS:
        target = orthos / name
        if target.exists():
            shutil.rmtree(target)


def build(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Run dpkg-buildpackage using debian/ from the source repo."""
    repo = Path(meta["repo_path"])
    # Allow callers (e.g. smoke) to redirect the orthos workspace so that
    # build-result.json and artifacts land in the original repo's .orthos dir
    # even when building from an isolated source copy.
    if "_orthos_override" in meta:
        orthos = Path(meta["_orthos_override"])
    else:
        orthos = orthos_dir(repo)

    dest_debian = repo / "debian"
    if not dest_debian.exists():
        raise FileNotFoundError(
            f"repo debian/ not found: {dest_debian}\n"
            f"Run 'orthos-packager apply {repo}' first."
        )

    logs_dir = orthos / "logs"
    ensure_dir(logs_dir)
    log_file = logs_dir / "build.log"
    log_file.write_text("", encoding="utf-8")  # truncate on each run

    info("using repo debian/ for build")

    ok, _ = run_logged(
        ["dpkg-buildpackage", "-us", "-uc", "-b"],
        log_file=log_file,
        cwd=repo,
        env=_clean_env(),
    )

    if ok:
        clean_debian_build_artifacts(repo)
        source_name: str = meta.get("project_name") or repo.name
        artifacts = _retain_artifacts(repo, orthos, source_name)
        _cleanup_transient(orthos)

        # Artifact dependency validation: inspect every retained .deb and
        # fail the build if any Depends group is not resolvable in the
        # target apt database.
        # Generated sibling packages are exempt (they are not in apt yet).
        deb_artifacts = [p for p in artifacts if p.endswith(".deb")]
        if deb_artifacts:
            gen_result_file = orthos / "generate-result.json"
            generated_pkg_names: frozenset[str] = frozenset()
            if gen_result_file.exists():
                try:
                    gen_result = json.loads(
                        gen_result_file.read_text(encoding="utf-8"))
                    generated_pkg_names = frozenset(
                        gen_result.get("binary_packages", []))
                except (json.JSONDecodeError, OSError):
                    pass  # proceed with empty sibling set

            # Select oracle: use the target chroot when available so that
            # validation uses the target Debian database, not the host's.
            chroot_path = meta.get("_chroot_path")
            oracle = make_oracle(chroot_path)
            info(f"artifact validation oracle: {oracle!r}")

            try:
                validate_built_debs(deb_artifacts, generated_pkg_names, oracle)
            except RuntimeError as exc:
                error(str(exc))
                ok = False  # treat as build failure
    else:
        artifacts = []

    result: dict[str, Any] = {
        "artifacts": artifacts,
        "artifacts_dir": str(orthos / "artifacts") if ok else None,
        "generated_debian_dir": str(orthos / "debian"),
        "target_debian_dir": str(dest_debian),
        "used_repo_debian": True,
        "used_generated_debian": False,
        "failure_step": None if ok else "dpkg-buildpackage",
        "log_file": str(log_file),
        "repo_path": str(repo),
        "success": ok,
    }

    write_json(orthos / _RESULT_FILE, result)
    return (0 if ok else 1), result
    