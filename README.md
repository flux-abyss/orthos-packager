# Orthos Packager

Orthos Packager is a deterministic Debian packaging pipeline for building .deb packages from source repositories.

It analyzes a project (currently Meson-based), stages an install tree, inventories the staged files, classifies them into Debian package roles, and generates a Debian package layout with a focus on transparency, reproducibility, and minimal manual setup.

---

## What it does

- Detects project structure (Meson-focused for now)
- Builds and stages files into a proper filesystem layout
- Inventories staged files with semantic labels for runtime, data, development files, app-private plugins, helpers, desktop metadata, services, and config seeds
- Generates a Debian `debian/` directory
- Generates functional Debian packaging files including `control`, `rules`, `changelog`, `source/format`, and DEP-5-style `copyright`
- Produces installable `.deb` packages
- Supports isolated target-aware dependency convergence through a Debian chroot
- Supports chroot-based staging and chroot-based package builds
- Builds from an isolated source copy and injects generated `debian/` packaging cleanly
- Preserves special permissions such as setuid/setgid through generated maintainer scripts
- Supports a local maintainer identity config for generated Debian metadata
- Extracts upstream metadata for package descriptions, license data, copyright notices, upstream contact, and source URLs where available

---

## Pipeline

Orthos runs in clear, inspectable stages:

scan → stage → inventory → classify → generate → build

Each stage outputs artifacts under `.orthos/` for debugging and reproducibility.

For full target-aware validation, use package mode. Package mode runs convergence in a target chroot, stages in the chroot, generates packaging, builds from an isolated source copy, and collects build artifacts.

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
deb package .
```

For a Debian target chroot:

```bash
deb package . --target-repo-set debian
```

Pass Meson options when needed:

```bash
deb package . --target-repo-set debian --meson-option wl=true
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

## Generated Debian Metadata

Orthos generates practical Debian packaging scaffolds rather than empty placeholder files.

Generated output currently includes:

- `debian/control`
- `debian/rules`
- `debian/changelog`
- `debian/source/format`
- `debian/copyright`

Where possible, Orthos fills these files from discovered project metadata instead of inventing values. Metadata sources include Meson project fields, desktop files, README files, legal files, author files, and git origin.

The generated `copyright` file follows a DEP-5-style layout and can include upstream name, upstream contact, source URL, copyright notices, declared license name, upstream license text, Debian packaging copyright, and Debian packaging license.

The generated `rules` file is a functional debhelper/Meson rules file. It includes Meson configuration passthrough for options supplied through `--meson-option`.

These files are intended to be useful maintainer scaffolds. They are generated as accurately as Orthos can infer from the source tree, but human review is still expected before publication.

---

## Current Status

Orthos can successfully package real-world Meson applications, including EFL-based projects like Evisum and Enlightenment.

Current milestones include:

- Target-aware chroot dependency convergence
- Chroot-based staging for target-correct Meson setup, compile, and install
- Chroot-based Debian package builds
- Isolated source builds with generated `debian/` packaging injected cleanly
- Clean single-package generation for simple Meson applications
- Multi-package generation for larger desktop projects with runtime, data, development, and plugin-oriented package roles
- CLI support for passing optional Meson build flags into the packaging flow
- Semantic classification for runtime files, development files, app-private data, plugins, helpers, desktop metadata, session metadata, services, and config seeds
- Runtime dependency provenance tracking through ELF dynamic dependency inspection
- Build dependency inference through Meson dependency mapping
- Generated Debian `control` files with inferred build dependencies, runtime dependency handling, package relationships, and maintainer identity
- Generated debhelper/Meson `rules` files with Meson option passthrough
- Generated DEP-5-style copyright scaffolds using upstream legal metadata where available
- Upstream metadata probing from Meson, desktop files, README files, legal files, author files, and git origin
- Setuid/setgid preservation for privileged helper binaries during package installation
- Configurable Debian maintainer identity for generated `control`, `changelog`, and copyright metadata

Focus is now shifting from "can it build" to generating clean, maintainer-quality Debian packaging.

## Notes

- Currently optimized for Meson-based projects
- Packaging output is functional, but still evolving toward Debian best practices
- `.orthos/` is used as a working directory and is not part of source output
- Package mode is the preferred path for target-aware package validation
- Standalone stages remain useful for debugging, but may rely on host context unless a target chroot is threaded through the command path

## License

GPL-3.0