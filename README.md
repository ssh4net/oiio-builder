# Build Libraries (Python Builder)

This repo provides a cross-platform Python builder that mirrors the behavior of the existing Bash stack script. It clones/updates repos, builds static libraries (or system/dynamic where needed), and installs into per-config prefixes on macOS/Linux or a shared prefix on Windows.

## Quick Start

```bash
# Create venv and run the builder
uv venv
uv run build.py --list-repos
uv run build.py --print-prefixes
```

### Run a small build
```bash
uv run build.py --build-types Debug,Release --only zlib-ng,libpng
```

## Configuration

The builder reads `build.toml` from the repo root.

Key options:
- `prefix_base`: base install prefix on Unix. Debug/ASAN add `d`/`a` suffixes.
- `build_types`: list of configs to build (`Debug`, `Release`, `ASAN`).
- `use_libcxx`: default on macOS/Linux; set `false` to use libstdc++.
- `build_*` toggles: enable/disable stacks (GL, EXR, image IO, etc.).
- `windows.generator`: choose one of `msvc`, `ninja-msvc`, `msvc-clang-cl`, `ninja-clang-cl`.
- `windows.install_prefix`: single prefix for Debug+Release on Windows.
- `windows.asan_prefix`: optional separate prefix for ASAN.

## Prefix Rules

- macOS/Linux:
  - `prefix_base=/mnt/f/UBS` → Release: `/mnt/f/UBS`, Debug: `/mnt/f/UBSd`, ASAN: `/mnt/f/UBSa`
- Windows:
  - Debug and Release share one prefix (debug builds first).
  - ASAN can use a separate prefix (e.g., `E:\\DVS_ASAN`).

## Common Commands

```bash
# List repos to build
uv run build.py --list-repos

# Print computed install prefixes
uv run build.py --print-prefixes

# Force rebuild
uv run build.py --force

# Build only specific repos
uv run build.py --only libjpeg-turbo,libpng,openjpeg

# Skip certain repos
uv run build.py --skip libwebp,libheif
```

## Notes

- The builder uses stamps in `../_build_py/.stamps` to skip rebuilds when
  no repo/toolchain/flag changes are detected.
- Uncommitted working tree changes are not detected yet (use `--force` if needed).
- Optional repos (e.g., `yaml-cpp`, `pystring`, `pugixml`, `expat`) are skipped if missing.

## Legacy Script

The original Bash script is still present at `build_MOS_stack_until_OIIO.sh` and remains the reference for options and build ordering.
