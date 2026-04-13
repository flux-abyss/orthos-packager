# Orthos Packager

Orthos Packager is a deterministic Debian packaging pipeline for building `.deb` packages from source repositories with minimal assumptions.

It is designed to take a project (currently Meson-based), analyze it, stage an install tree, and generate a working Debian package layout automatically.

---

## What it does

- Detects project structure (Meson-focused for now)
- Builds and stages files into a proper filesystem layout
- Generates a Debian `debian/` directory
- Produces installable `.deb` packages

---

## Pipeline

Orthos runs in clear, inspectable stages:

scan → stage → inventory → classify → generate → build

Each stage outputs artifacts under `.orthos/` for debugging and reproducibility.

---

## Usage

From a project directory:

```bash
debcraft scan .
debcraft stage .
debcraft inventory .
debcraft classify .
debcraft generate .
debcraft build .
```

Or run the full pipeline:

```bash
debcraft smoke .
```

## Current Status

Orthos can successfully package real-world Meson applications (including EFL-based projects like evisum).

Focus is now shifting from "can it build" to generating clean, maintainer-quality Debian packaging.

## Notes

- Currently optimized for Meson-based projects
- Packaging output is functional, but still evolving toward Debian best practices
- `.orthos/` is used as a working directory and is not part of source output

## License

GPL-3.0