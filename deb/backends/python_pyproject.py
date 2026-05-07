"""Python pyproject.toml (setuptools.build_meta) backend adapter.

Milestone B: detection and metadata extraction.
Milestone C: chroot staging via stage_deps() and stage_chroot().

host-mode stage() is intentionally not implemented and raises NotImplementedError.
All Python staging happens inside the selected chroot only.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

name = "python-pyproject"

# The only setuptools build-backend token recognised in this milestone.
_SETUPTOOLS_BUILD_META = "setuptools.build_meta"


# ---------------------------------------------------------------------------
# TOML loading
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file and return its contents as a dict.

    Uses stdlib tomllib (Python >=3.11).  On Python 3.10 the import will
    fail with a clear ImportError explaining the Debian dependency.
    """
    if sys.version_info >= (3, 11):
        import tomllib  # noqa: PLC0415
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    else:
        # Python 3.10: tomllib is not in stdlib.  Attempt tomli (common
        # third-party shim) and surface a clear error if unavailable.
        try:
            import tomli as tomllib  # type: ignore[no-redef]  # noqa: PLC0415
        except ModuleNotFoundError:
            raise ImportError(
                "Python 3.10 detected: 'tomli' is required to parse pyproject.toml. "
                "Install the Debian package python3-tomli, or run Orthos with Python >= 3.11."
            ) from None
        with open(path, "rb") as fh:
            return tomllib.load(fh)


# ---------------------------------------------------------------------------
# Registry protocol
# ---------------------------------------------------------------------------

def can_handle(repo: Path) -> bool:
    """Return True when *repo* has a pyproject.toml using setuptools.build_meta.

    Checks:
    1. pyproject.toml exists.
    2. [build-system].build-backend == "setuptools.build_meta".

    Poetry, Hatch, Flit, and custom backends are intentionally excluded.
    """
    toml_path = repo / "pyproject.toml"
    if not toml_path.exists():
        return False
    try:
        data = _load_toml(toml_path)
    except Exception:  # noqa: BLE001 — malformed TOML, wrong Python, etc.
        return False
    build_sys = data.get("build-system", {})
    return build_sys.get("build-backend") == _SETUPTOOLS_BUILD_META


def scan_metadata(repo: Path) -> dict[str, Any]:
    """Return pyproject metadata for *repo*.

    Keys always returned:
        build_backend        - "python-pyproject"
        python               - True
        python_build_backend - "setuptools.build_meta"
        project_name         - [project].name, or None
        version              - [project].version, or None
        version_source       - "pyproject" when version found, else "fallback"

    Optional keys (only present when the field exists in pyproject.toml):
        description          - [project].description
        requires_python      - [project].requires-python
        scripts              - {name: entrypoint, …} from [project.scripts]
    """
    toml_path = repo / "pyproject.toml"
    data = _load_toml(toml_path)
    project = data.get("project", {})

    project_name: str | None = project.get("name") or None
    version: str | None = project.get("version") or None
    version_source = "pyproject" if version else "fallback"

    meta: dict[str, Any] = {
        "build_backend": name,
        "python": True,
        "python_build_backend": _SETUPTOOLS_BUILD_META,
        "project_name": project_name,
        "version": version,
        "version_source": version_source,
    }

    description: str | None = project.get("description") or None
    if description is not None:
        meta["description"] = description

    requires_python: str | None = project.get("requires-python") or None
    if requires_python is not None:
        meta["requires_python"] = requires_python

    scripts: dict[str, str] | None = project.get("scripts") or None
    if scripts is not None:
        meta["scripts"] = dict(scripts)

    return meta


def stage(meta: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Raise NotImplementedError — Python host staging is not implemented.

    Python staging happens inside the chroot only, via stage_chroot().
    orthos stage is not supported for python-pyproject projects.
    """
    raise NotImplementedError(
        "stage() is not supported for build_backend='python-pyproject'. "
        "Python project packaging is not implemented in this release."
    )


# ---------------------------------------------------------------------------
# Chroot staging (Milestone C)
# ---------------------------------------------------------------------------

# Debian packages that must be installed inside the chroot before
# stage_chroot() can run.  Installed by _run_chroot_stage after convergence.
_STAGE_DEPS: list[str] = [
    "python3-build",
    "python3-installer",
    "python3-wheel",
    "python3-setuptools",
]


def stage_deps() -> list[str]:
    """Return Debian packages required inside the chroot before staging."""
    return list(_STAGE_DEPS)


def stage_chroot(
    meta: dict[str, Any],
    chroot_exec_fn,
    chroot_root: "Path",
    source_path: str,
    build_path: str,
    destdir_path: str,
    log_file: "Path",
) -> tuple[bool, str]:
    """Run python3-build + python3-installer inside an already-mounted chroot.

    /orthos/source is mounted read-only.  setuptools writes egg-info and other
    build metadata into the source tree during wheel build, so we copy the
    source to a writable location (/orthos/build/src) before building.

    Steps:
      0. Clean /orthos/build/src, /orthos/build/dist, /orthos/build/destdir.
      1. Copy /orthos/source into /orthos/build/src.
      2. python3 -m build --wheel --no-isolation --outdir /orthos/build/dist
         (run from /orthos/build/src, not /orthos/source).
      3. Resolve exactly one *.whl (fail clearly if zero or ambiguous).
      4. python3 -m installer --destdir DESTDIR --prefix /usr <wheel>
      5. Verify no files were installed under /usr/local.

    Arguments and return value follow the same contract as meson.stage_chroot().
    """
    src_path = f"{build_path}/src"
    dist_path = f"{build_path}/dist"

    # Step 0: clean all writable build outputs so nothing stale can leak through.
    clean_cmd = [
        "bash", "-c",
        f"rm -rf {src_path} {dist_path} {destdir_path}"
        f" && mkdir -p {src_path} {dist_path}",
    ]
    ok, output = chroot_exec_fn(chroot_root, clean_cmd)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# clean build dirs\n{output}")
    if not ok:
        return False, "clean build dirs"

    # Step 1: copy read-only source into writable /orthos/build/src.
    # cp -a preserves symlinks and permissions; trailing /. copies contents.
    copy_cmd = ["bash", "-c", f"cp -a {source_path}/. {src_path}/"]
    ok, output = chroot_exec_fn(chroot_root, copy_cmd)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# copy source\n{output}")
    if not ok:
        return False, "copy source"

    # Step 2: build wheel from the writable copy (--no-isolation uses chroot deps).
    build_cmd = [
        "bash", "-c",
        f"cd {src_path} && python3 -m build --wheel --no-isolation"
        f" --outdir {dist_path}",
    ]
    ok, output = chroot_exec_fn(chroot_root, build_cmd)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# build wheel\n{output}")
    if not ok:
        return False, "build wheel"

    # Step 3: resolve exactly one wheel; fail clearly if none or multiple found.
    find_cmd = ["bash", "-c", f"ls {dist_path}/*.whl 2>/dev/null"]
    _ok, wheel_output = chroot_exec_fn(chroot_root, find_cmd)
    wheels = [w.strip() for w in wheel_output.strip().splitlines() if w.strip()]
    if not wheels:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write("\n# no wheel found\n")
        return False, "no wheel produced"
    if len(wheels) > 1:
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"\n# multiple wheels found: {wheels}\n")
        return False, "multiple wheels produced (exactly one expected)"
    wheel = wheels[0]

    # Step 4: install into DESTDIR using Debian layout.
    # DEB_PYTHON_INSTALL_LAYOUT=deb instructs python3-installer to use the
    # Debian scheme: /usr/lib/python3/dist-packages instead of /usr/local/...
    install_cmd = [
        "bash", "-c",
        f"DEB_PYTHON_INSTALL_LAYOUT=deb"
        f" python3 -m installer --destdir {destdir_path} --prefix /usr {wheel}",
    ]
    ok, output = chroot_exec_fn(chroot_root, install_cmd)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# install wheel\n{output}")
    if not ok:
        return False, "install wheel"

    # Step 5: verify no /usr/local leakage.
    # First list any offending files into the log so failures are diagnosable.
    list_local_cmd = [
        "bash", "-c",
        f"find {destdir_path}/usr/local -mindepth 1 2>/dev/null || true",
    ]
    _ok, local_output = chroot_exec_fn(chroot_root, list_local_cmd)
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"\n# verify no /usr/local\n{local_output}")
    if local_output.strip():
        return False, "unexpected /usr/local files in DESTDIR"

    return True, ""
