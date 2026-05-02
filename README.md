# Orthos Packager

Orthos Packager is a deterministic Debian packaging pipeline for building .deb packages from source repositories.

It analyzes a project (currently Meson-based), stages an install tree, inventories the staged files, classifies them into Debian package roles, and generates a Debian package layout with a focus on transparency, reproducibility, and minimal manual setup.

---

## What it does

- Detects project structure (Meson-focused for now)
- Builds and stages files into a proper filesystem layout
- Inventories staged files with semantic labels for runtime, data, development files, app-private plugins, helpers, desktop metadata, services, and config seeds
- Generates a Debian `debian/` directory
- Produces installable `.deb` packages
- Supports isolated target-aware dependency convergence through a Debian chroot
- Preserves special permissions such as setuid/setgid through generated maintainer scripts
- Supports a local maintainer identity config for generated Debian metadata

---

## Pipeline

Orthos runs in clear, inspectable stages:

scan → stage → inventory → classify → generate → build

Each stage outputs artifacts under `.orthos/` for debugging and reproducibility.

For full target-aware validation, use smoke mode. Smoke runs convergence in a target chroot, generates packaging, builds from an isolated source copy, and collects build artifacts.

---

## Usage

From a project directory:

```bash
deb scan .
deb stage .
deb inventory .
deb classify .
deb generate .
deb build .
```

Or run the full pipeline:

```bash
deb smoke .
```

For a Debian target chroot:

```bash
deb smoke . --target-repo-set debian
```

Set local maintainer identity once:

```bash
deb config init
deb config show
```

The maintainer config is stored at:

```text
~/.config/orthos/orthos.toml
```

---

## Current Status

Orthos can successfully package real-world Meson applications, including EFL-based projects like evisum and Enlightenment.

Current milestones include:

- Target-aware chroot dependency convergence
- Clean Enlightenment package generation against Debian 13 / Trixie
- Debian-like Enlightenment package topology:
  - `enlightenment`
  - `enlightenment-data`
  - `enlightenment-dev`
- Semantic classification for app-private runtime files, plugin metadata, helpers, session metadata, desktop launchers, services, and config seeds
- Setuid preservation for privileged helper binaries during package installation
- Configurable Debian maintainer identity for generated `control`, `changelog`, and copyright metadata

Focus is now shifting from "can it build" to generating clean, maintainer-quality Debian packaging.

## Notes

- Currently optimized for Meson-based projects
- Packaging output is functional, but still evolving toward Debian best practices
- `.orthos/` is used as a working directory and is not part of source output
- Smoke mode is the preferred path for target-aware package validation
- Standalone stages remain useful for debugging, but may rely on host context unless a target chroot is threaded through the command path

## License

GPL-3.0
