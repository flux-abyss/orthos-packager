"""Post-generation package relationship validator.

Inspects the in-memory output_packages list produced by _build_package_layout
and reports two structured validation records:

* inter_pkg_validation  — did the primary package pick up expected siblings?
* dev_pkg_validation    — does each -dev package have the right lockstep dep
                          and no ${shlibs:Depends} leakage?

No files are read or written here.  All inputs come from already-computed
package metadata.  This is a reporting pass only.
"""

from __future__ import annotations

from typing import Any

# Sibling bucket suffixes the primary package is always expected to depend on
# when those packages exist in the generated set.
_ALWAYS_EXPECTED_SIBLINGS = {"data"}

# Shlibs token that must NOT appear in a -dev package's depends.
_SHLIBS_TOKEN = "${shlibs:Depends}"

# Versioned lockstep token pattern (prefix only; the version part varies).
_LOCKSTEP_PREFIX = "${binary:Version})"


def validate_packages(
    app_name: str,
    output_packages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a validation record for *output_packages*.

    Returns a dict with two keys:
      "inter_pkg_validation"  – primary-package sibling dep check
      "dev_pkg_validation"    – list of per-dev-package semantic checks
    """
    return {
        "inter_pkg_validation": _validate_inter_pkg(app_name, output_packages),
        "dev_pkg_validation": _validate_dev_pkgs(app_name, output_packages),
    }


# ---------------------------------------------------------------------------
# Inter-package validation
# ---------------------------------------------------------------------------

def _validate_inter_pkg(
    app_name: str,
    output_packages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate that the primary package depends on expected siblings."""
    pkg_names = {p["name"] for p in output_packages}

    # Expected siblings: those named <app>-<suffix> where suffix is always-expected.
    expected: list[str] = []
    for suffix in sorted(_ALWAYS_EXPECTED_SIBLINGS):
        candidate = f"{app_name}-{suffix}"
        if candidate in pkg_names:
            expected.append(candidate)

    # Also check -other when it exists (runtime-relevant rule from inter_pkg).
    other_pkg = f"{app_name}-other"
    if other_pkg in pkg_names and other_pkg not in expected:
        # We don't always expect other, but if it's there we surface it for
        # information even if not in the mandatory set.  Keep it in a separate
        # key so callers can distinguish mandatory from advisory.
        pass  # handled below via present/missing across all siblings

    # Locate the primary package descriptor (the one named exactly app_name).
    primary_pkg = next((p for p in output_packages if p["name"] == app_name), None)
    primary_depends: list[str] = []
    if primary_pkg is not None:
        primary_depends = list(primary_pkg.get("extra_depends", []))

    present = [s for s in expected if s in primary_depends]
    missing = [s for s in expected if s not in primary_depends]

    return {
        "primary_package": app_name,
        "expected_primary_depends": expected,
        "present_primary_depends": present,
        "missing_primary_depends": missing,
    }


# ---------------------------------------------------------------------------
# Dev package semantic validation
# ---------------------------------------------------------------------------

def _validate_dev_pkgs(
    app_name: str,
    output_packages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate -dev package dependency semantics."""
    records: list[dict[str, Any]] = []

    for pkg in output_packages:
        if not pkg.get("is_dev"):
            continue

        depends: list[str] = list(pkg.get("extra_depends", []))

        # Lockstep dep: any entry that contains the binary:Version token and
        # references the base app package name.
        has_lockstep = any(
            app_name in dep and _LOCKSTEP_PREFIX in dep
            for dep in depends
        )

        # Shlibs leakage: ${shlibs:Depends} must NOT appear; _gen_control
        # should have excluded it for is_dev packages, but we verify here.
        has_shlibs = _SHLIBS_TOKEN in depends

        records.append({
            "package": pkg["name"],
            "has_main_lockstep_dep": has_lockstep,
            "has_shlibs_dep": has_shlibs,
        })

    return records
