# Orthos

Orthos is a deterministic Debian packaging tool.

It scans a source repository, stages an install tree, inventories the staged files, classifies package contents, generates a `debian/` layout, builds `.deb` artifacts, and writes a readable package report.

Orthos is currently focused on Meson projects and builds through isolated Debian chroots.

## What Orthos does

- Detects supported source projects
- Runs dependency convergence in a target chroot
- Stages the project install tree in the chroot
- Inventories staged files
- Classifies runtime, data, development, desktop, service, config, helper, and plugin files
- Generates Debian packaging files
- Builds packages with `dpkg-buildpackage` inside the chroot
- Copies finished `.deb` artifacts into `.orthos/<project>/artifacts/`
- Writes a package report at `.orthos/<project>/package-report.txt`

## Pipeline

`scan -> stage -> inventory -> classify -> generate -> build -> report`

The full user-facing path is:

`orthos package /path/to/project`

Use an explicit Debian target profile:

`orthos package /path/to/project --target-repo-set debian`

Use the Debian+Bodhi overlay target profile:

`orthos package /path/to/project --target-repo-set debodhi`

Pass Meson options when needed:

`orthos package /path/to/project --target-repo-set debian --meson-option wl=true`

Reset a target chroot:

`orthos reset-chroot /path/to/project --target-repo-set debian`

## Target repo profiles

By default, Orthos uses the native target label, meaning no explicit target repo overlay is selected.

`no --target-repo-set          -> native label, no explicit repo overlay`

`--target-repo-set debian      -> explicit Debian target profile`

`--target-repo-set debodhi     -> Debian-compatible base with Bodhi repo overlay`

The native label does not copy host apt sources into the chroot.

## Maintainer identity

Set local maintainer metadata once:

`orthos config init`

`orthos config show`

The config is stored at:

`~/.config/orthos/orthos.toml`

## Generated Debian files

Orthos currently generates:

- `debian/control`
- `debian/rules`
- `debian/changelog`
- `debian/source/format`
- `debian/copyright`
- package `.install` files
- maintainer scripts when needed
- lintian overrides when needed

Generated packaging is intended to be useful scaffolding. Human review is still expected before publication.

## Working directory

Orthos writes its working files under:

`.orthos/`

This directory contains generated metadata, staged files, logs, chroots, build workspaces, artifacts, and package reports. It is not source output.

## Current status

Orthos can package real Meson projects, including Evisum and Enlightenment.

The current focus is clean, target-aware Debian package generation through chroot-based builds.

## License

GPL-3.0