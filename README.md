# Build Libraries (Python Builder)

This repo provides a cross-platform Python builder that mirrors the behavior of the existing Bash stack script. It clones/updates repos, builds static libraries (or system/dynamic where needed), and installs into per-config prefixes on macOS/Linux or a shared prefix on Windows.

## Quick Start

```bash
# Create venv and run the builder
uv venv
uv run build.py --list-repos
uv run build.py --print-prefixes
```

### Preflight (no args)
```bash
# No arguments runs a tool/repo preflight and exits
uv run build.py
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
- `windows.msvc_runtime`: `static` (default, `/MT`/`/MTd`) or `dynamic` (`/MD`/`/MDd`).
- `windows.python_wrappers`: `auto` (default), `on`, `off` for OpenColorIO/OpenEXR Python bindings.
  `auto` enables wrappers only when `windows.msvc_runtime=dynamic`.
- `windows.env`: tool overrides for Windows (e.g. `PKG_CONFIG_EXECUTABLE`, `DOXYGEN_EXECUTABLE`).

## Prefix Rules

- macOS/Linux:
  - `prefix_base=/mnt/f/UBS` → Release: `/mnt/f/UBS`, Debug: `/mnt/f/UBSd`, ASAN: `/mnt/f/UBSa`
- Windows:
  - Debug and Release share one prefix (debug builds first).
  - ASAN can use a separate prefix (e.g., `E:\\DVS_ASAN`).

## Common Commands

```bash
# Preflight checks (tools + repos)
uv run build.py --preflight

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

## Platform Examples

### macOS (Apple Clang + libc++)
```bash
# Example: set base prefix and OpenMP (Homebrew)
export OpenMP_ROOT=/opt/homebrew/opt/libomp
uv run build.py --build-types Debug,Release
```

### Linux (clang + libc++, or libstdc++)
```bash
# libc++ (default)
uv run build.py --build-types Debug,Release

# libstdc++ (set in build.toml: use_libcxx = false)
uv run build.py --build-types Debug,Release
```

### Windows (Visual Studio + clang-cl or MSVC)
```bat
:: Ninja + clang-cl
uv run build.py --config build.toml --build-types Debug,Release

:: Visual Studio solution + clang-cl
:: (set windows.generator = \"msvc-clang-cl\" in build.toml)
uv run build.py --build-types Debug,Release
```

### Windows: libiconv (for libxml2)
On Windows, `libiconv` is imported from a **vcpkg export zip** (no source build).

- Default path: `external/vcpkg-export-libiconv.zip`
- Override: set `LIBICONV_VCPKG_EXPORT_ZIP` in `[windows.env]` (or process env)
- Prefer exporting a `*-static` triplet (e.g. `x64-windows-static`) to avoid DLL collisions in the shared prefix.

Example:
```bat
vcpkg export libiconv:x64-windows-static --zip --output=vcpkg-export-libiconv
```

### Tool overrides (Windows)
```toml
[windows.env]
PKG_CONFIG_EXECUTABLE = "C:\\msys64\\usr\\bin\\pkg-config.exe"
DOXYGEN_EXECUTABLE = "C:\\Program Files\\doxygen\\bin\\doxygen.exe"
```

## Troubleshooting

- **Rebuild not triggered after local edits**: stamps track git commits only. Use `--force` or delete `../_build_py/.stamps`.
- **Missing optional repos**: `yaml-cpp`, `pystring`, `expat`, `pugixml`, `libxml2` are skipped if not present. On Windows, `libiconv` is expected via `external/vcpkg-export-libiconv.zip`.
- **OpenMP not found (macOS/Linux)**: set `OpenMP_ROOT` in `build.toml` or environment.
- **ASAN failures on Windows**: prefer clang-cl and ensure the MSVC AddressSanitizer component is installed.
- **PyOpenColorIO / PyOpenEXR link errors on Windows**: set `windows.msvc_runtime = "dynamic"` and `windows.python_wrappers = "on"` for wrapper builds.
- **Preflight only**: run `uv run build.py` (no args) to see tool/repo readiness without building.

## Notes

- The builder uses stamps in `../_build_py/.stamps` to skip rebuilds when
  no repo/toolchain/flag changes are detected.
- Uncommitted working tree changes are not detected yet (use `--force` if needed).
- Optional repos (e.g., `yaml-cpp`, `pystring`, `pugixml`, `expat`) are skipped if missing.

## Legacy Script

The original Bash script is still present at `build_MOS_stack_until_OIIO.sh` and remains the reference for options and build ordering.
