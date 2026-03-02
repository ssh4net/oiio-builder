# Build Libraries (Python Builder)

This repo provides a cross-platform Python builder that mirrors the behavior of the existing Bash stack script. It clones/updates repos, builds static libraries (or system/dynamic where needed), and installs into per-config prefixes on macOS/Linux or a shared prefix on Windows.

## Prerequisites

Tools the builder expects to find (or be explicitly pointed at via config/env):
- `git`
- `cmake`
- `ninja` (for Ninja-based generators)
- `pkg-config` (we recommend `pkgconf`)
- `ccache` (optional, recommended on macOS/Linux)
- `doxygen`
- OpenMP runtime (`libomp`) when enabling OpenMP (for example: `libraw_enable_openmp="ON"`). On Windows this commonly comes from an LLVM install.
- `nasm`/`yasm` on x86_64
- Python 3.11+ (uses `tomllib`)

We recommend using `uv` (Astral) to create the virtual environment and run commands in a reproducible way, but any Python 3.11+ venv works. The builder itself is stdlib-only (no mandatory pip dependencies).

Windows notes:
- Doxygen and LLVM can be installed from official/prebuilt installers (common layout: `C:\\Program Files\\doxygen\\...`, `C:\\LLVM\\...`).
- `pkg-config` can be obtained via vcpkg (the builder does not use vcpkg itself, but it is useful for tools and for a few Windows-only imports like `libiconv`). You can point `PKG_CONFIG_EXECUTABLE` at the vcpkg-installed `pkgconf.exe`, or `vcpkg export pkgconf --zip` and unpack it anywhere on disk.

`ccache` install (optional, macOS/Linux):
- Ubuntu/Debian: `sudo apt-get install ccache`
- Fedora/RHEL: `sudo dnf install ccache`
- Arch: `sudo pacman -S ccache`
- macOS (Homebrew): `brew install ccache`
- Verify: `ccache --version`

Linux GTK3 headers (needed for `nativefiledialog-extended` when `NFD_PORTAL=OFF`):
- Ubuntu/Debian: `sudo apt-get install pkg-config libgtk-3-dev`
- Verify: `pkg-config --modversion gtk+-3.0`

## Installation (Step-by-Step)

1. Install the prerequisites above (via Homebrew/apt/choco/winget/etc.).
2. Clone this repo and enter it.
3. Create a Python environment (recommended: `uv`):
   ```bash
   uv venv
   ```
4. Optional: if you want `sphinx-build` available (docs tooling), install it into the venv:
   ```bash
   uv pip install sphinx
   ```
5. (Optional, recommended) Create `build.user.toml` for local overrides (gitignored). Example for Windows tool paths:
   ```toml
   [windows.env]
   PKG_CONFIG_EXECUTABLE = "E:/vcpkg/installed/x64-windows/tools/pkgconf/pkgconf.exe"
   DOXYGEN_EXECUTABLE = "C:/Program Files/doxygen/bin/doxygen.exe"
   OpenMP_ROOT = "C:/LLVM" # provides <OpenMP_ROOT>/lib/libomp.lib
   ```
   Example for ccache on Linux/macOS:
   ```toml
   [global]
   use_ccache = true

   [global.env]
   CCACHE_DIR = "/tmp/ccache"        # pick a fast local filesystem
   CCACHE_TEMPDIR = "/tmp/ccache-tmp"
   CCACHE_MAXSIZE = "20G"
   ```
6. Run a preflight check:
   ```bash
   uv run build.py --preflight
   ```
7. Build:
   ```bash
   uv run build.py --build-types Debug,Release
   ```
   Parallel build types on macOS/Linux (splits `--jobs` across configs):
   ```bash
   uv run build.py --build-types Debug,Release --parallel-build-types
   ```

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
- `src_root`: where repos are cloned (default in this repo: `./developer`).
- `build_root`: where per-repo build dirs and stamps live (default: `./developer/_build`).
- `prefix_layout`:
  - `by-build-type`: per-config prefixes (Unix: `Release/Debug/ASAN` subdirs; Windows: `install` + `asan`).
  - `suffix`: legacy Unix layout using `debug_suffix`/`asan_suffix`.
- `prefix_base`: prefix root used by `prefix_layout` (default in this repo: `./developer/install`).
- `install_prefix`: optional explicit install prefix (primarily for Windows); acts as default for `[windows].install_prefix`.
- `asan_prefix`: optional explicit ASAN prefix (primarily for Windows); acts as default for `[windows].asan_prefix`.
- `build_types`: list of configs to build (`Debug`, `Release`, `ASAN`).
- `preferred_repo_order`: optional list of repo names that influences build order when multiple repos are ready (deps still win).
- `use_libcxx`: default on macOS/Linux; set `false` to use libstdc++.
- `use_ccache`: enable `ccache` compiler launcher on macOS/Linux (default: `true`). Disable with `--no-ccache` or `use_ccache=false`. Configure cache paths via `[global.env]` (`CCACHE_DIR`, `CCACHE_TEMPDIR`, `CCACHE_MAXSIZE`, …).
- `build_*` toggles: enable/disable stacks (GL, EXR, image IO, etc.).
- `build_cpython`: build CPython from source (`https://github.com/python/cpython.git`).
  - On Windows: enabled by default.
  - On Linux/macOS: built only when `cpython_ref` is explicitly set.
- `sqlite` and `libffi` are built ahead of `cpython` when CPython is requested.
- `cpython_ref`: optional CPython git ref override (example: `3.13`, `v3.12.11`, commit SHA).
- `cpython_ref_type`: `branch` (default), `tag`, or `commit` for `cpython_ref`.
- `build_qt6`: build a minimal **static Qt6** stack into the prefix (for consumers like OpenImageIO `iv` and GPUpad).
- `build_dng_sdk`: build Adobe DNG SDK + XMP (via `DNG-CMake`) into the prefix (optional; disabled by default).
- `windows.generator`: choose one of `msvc`, `ninja-msvc`, `msvc-clang-cl`, `ninja-clang-cl`.
- `windows.vs_generator`: optional CMake generator name override for `windows.generator=msvc`/`msvc-clang-cl` (e.g. `Visual Studio 18 2026` with CMake 4.2+).
- `windows.install_prefix`: single prefix for Debug+Release on Windows.
- `windows.asan_prefix`: optional separate prefix for ASAN.
- `windows.build_ffmpeg`: defaults to `false`; when `true`, Windows builds use prebuilt FFmpeg by default, or native FFmpeg source build when run from MSYS2 (see below).
- `windows.msvc_runtime`: `static` (default, `/MT`/`/MTd`) or `dynamic` (`/MD`/`/MDd`).
- `windows.python_wrappers`: `auto` (default), `on`, `off` for OpenColorIO/OpenEXR Python bindings.
  `auto` enables wrappers only when `windows.msvc_runtime=dynamic`.
- `windows.cpython_fetch_externals`: `false` (default) passes `-E` to CPython `PCbuild/build.bat` (no external dependency downloads); `true` uses `-e`.
- On Windows, `sqlite`/`libffi` are imported from vcpkg export zips (`external/vcpkg-export-sqlite.zip`, `external/vcpkg-export-libffi.zip`) instead of source/autotools builds.
- `windows.clangcl_extra_flags`: clang-cl x86_64 baseline extra flags (default if unset: `-msse4.1`).
- `windows.clangcl_extra_flags_append`: extra clang-cl x86_64 flags appended to the baseline (default: empty).
- `windows.env`: tool overrides for Windows (e.g. `PKG_CONFIG_EXECUTABLE`, `DOXYGEN_EXECUTABLE`).

Windows prefix precedence:
- `windows.install_prefix` / `windows.asan_prefix` (highest)
- `global.install_prefix` / `global.asan_prefix`
- `global.prefix_base` (fallback)

### Repo Defaults and Local Overrides

Repo graphs and global policy live in `build.toml`, but per-repo default CMake cache settings live in
tracked files under `builder/recipes/defaults/<repo>.toml`.

Local overrides are read from `build.user.toml` (gitignored) and merged on top of `build.toml`
(CLI flags still win). You can override `[global]`, `[windows]`, and per-repo CMake cache settings.

```toml
[global]
prefix_base = "./developer/install" # example

[windows]
generator = "msvc"
vs_generator = "Visual Studio 18 2026"

[[repo_overrides]]
name = "libpng"

[repo_overrides.cmake.cache]
PNG_TESTS = true
```

## Prefix Rules

- macOS/Linux (`prefix_layout="by-build-type"`):
  - `prefix_base=/mnt/f/dev` → Release: `/mnt/f/dev/Release`, Debug: `/mnt/f/dev/Debug`, ASAN: `/mnt/f/dev/ASAN`
- macOS/Linux (`prefix_layout="suffix"`):
  - `prefix_base=/mnt/f/UBS` → Release: `/mnt/f/UBS`, Debug: `/mnt/f/UBSd`, ASAN: `/mnt/f/UBSasn`
- Windows:
  - Debug and Release share one prefix (debug builds first).
  - ASAN can use a separate prefix (e.g., `./developer/asan`).

## Install Markers (Prefix Retargeting)

The builder writes per-repo install markers under:

`<prefix>/.oiio-builder/install-stamps/<repo>/<build_type>.json`

If a repo is up-to-date but its marker is missing or mismatched (for example: you changed `prefix_base` or deleted/moved a
prefix directory), the builder automatically re-runs the repo install step instead of skipping it.
Use `--reinstall` / `--reinstall-all` to force reinstall even when markers are present.

## Common Commands

```bash
# Preflight checks (tools + repos)
uv run build.py --preflight

# List repos to build
uv run build.py --list-repos

# Print computed install prefixes
uv run build.py --print-prefixes

# Force rebuild
uv run build.py --force          # with --only: forces only selected repos
uv run build.py --force-all      # forces all repos in this run

# Force reinstall (install step only when up-to-date)
uv run build.py --reinstall         # with --only: reinstalls only selected repos
uv run build.py --reinstall-all     # reinstalls all repos in this run

# Build only specific repos
uv run build.py --only libjpeg-turbo,libpng,openjpeg

# Windows: build OIIO without FFmpeg
uv run build.py --build-types Debug --only OpenImageIO --no-ffmpeg

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

### Windows: FFmpeg
By default, `windows.build_ffmpeg = false` to keep OpenImageIO builds self-contained.

When `windows.build_ffmpeg = true`, the builder picks one of two modes:
1. **MSYS2 source-build mode** (auto): if `MSYSTEM`/MSYS2 is detected, FFmpeg is built from source via `bash + make` with `--toolchain=msvc`.
2. **Prebuilt mode** (fallback): if MSYS2 is not detected, install/copy an **MSVC-built static** FFmpeg into the same prefix used by this script (headers under `<prefix>/include`, libs under `<prefix>/lib`).

Notes:
- For `windows.generator = "ninja-clang-cl"` / `msvc-clang-cl`, FFmpeg is configured with `clang-cl`.
- Source-build mode requires `bash` and `make` in `PATH` (from MSYS2).

### Windows: libiconv (for libxml2)
On Windows, `libiconv` is imported from a **vcpkg export zip** (no source build).

- Default path: `external/vcpkg-export-libiconv.zip`
- Override: set `LIBICONV_VCPKG_EXPORT_ZIP` in `[windows.env]` (or process env)
- Prefer exporting a `*-static` triplet (e.g. `x64-windows-static`) to avoid DLL collisions in the shared prefix.

Example:
```bat
vcpkg export libiconv:x64-windows-static --zip --output=vcpkg-export-libiconv
```

### Windows: sqlite + libffi (for CPython)
On Windows, `sqlite` and `libffi` are imported from **vcpkg export zips** (no source/autotools build).

- Default paths:
  - `external/vcpkg-export-sqlite.zip`
  - `external/vcpkg-export-libffi.zip`
- Overrides (optional, via `[windows.env]` or process env):
  - `SQLITE_VCPKG_EXPORT_ZIP` (also accepts `SQLITE3_VCPKG_EXPORT_ZIP`)
  - `LIBFFI_VCPKG_EXPORT_ZIP`
- Prefer `*-static` triplets (e.g. `x64-windows-static`) to avoid DLL collisions in the shared prefix.

Examples:
```bat
vcpkg export sqlite3:x64-windows-static --zip --output=vcpkg-export-sqlite
vcpkg export libffi:x64-windows-static --zip --output=vcpkg-export-libffi
```

### Qt6 (static, optional)

Enable Qt6 builds by setting `build_qt6 = true` (recommended: in `build.user.toml`):
```toml
[global]
build_qt6 = true
```

What it builds (static):
- `qtbase`, `qtdeclarative` (includes Quick Controls in Qt6), `qtshadertools`, `qtmultimedia`, `qtimageformats`, `qtsvg` (+ `qtwayland` on Linux)

Build only Qt6:
```bash
uv run build.py --build-types Debug,Release --only Qt6
```

Skip Qt6 (build everything else):
```bash
uv run build.py --build-types Debug,Release --skip Qt6
```

Windows: OpenSSL import (required)
- Default expected path: `external/vcpkg-export-openssl.zip`
- Override: `OPENSSL_VCPKG_EXPORT_ZIP` in `[windows.env]` (or process env)

Example:
```bat
vcpkg export openssl:x64-windows-static --zip --output=vcpkg-export-openssl
```

Linux notes (XCB + Wayland)
- Qt is configured to build both XCB and Wayland QPA backends (`-qpa xcb;wayland`), so you need the relevant system development packages and `wayland-scanner` in `PATH`.

### Adobe DNG SDK + XMP (optional)

This enables LibRaw's optional Adobe DNG SDK integration (`USE_DNGSDK`) by building the SDK via `DNG-CMake` and linking it into `libraw`.

Enable it (recommended: in `build.user.toml`):
```toml
[global]
build_dng_sdk = true
```

Provide the Adobe DNG SDK sources (the builder does not vendor them):
- Default search: `external/dng_sdk_1_7_1_0.zip` (also `*.tar.gz` / extracted dir)
- Override: set `DNGSDK_ARCHIVE` to an archive path or extracted directory

Build a minimal set:
```bash
uv run build.py --build-types Debug,Release --only dng-sdk,libraw,OpenImageIO
```

### Tool overrides (Windows)
```toml
[windows.env]
PKG_CONFIG_EXECUTABLE = "C:\\msys64\\usr\\bin\\pkg-config.exe"
DOXYGEN_EXECUTABLE = "C:\\Program Files\\doxygen\\bin\\doxygen.exe"
```

## Troubleshooting

- **Rebuild not triggered after local edits**: stamps track dependency fingerprints and applied per-repo option layers, but not uncommitted working tree changes. Use `--force --only <repo>` for targeted rebuilds or `--force-all` for a clean run.
- **uv cache permission issues**: set `UV_CACHE_DIR` to a writable directory (e.g. `UV_CACHE_DIR=/tmp/uv-cache`).
- **nativefiledialog-extended (Linux) missing/broken GTK deps**: the builder configures `nativefiledialog-extended` with the GTK3 backend (`NFD_PORTAL=OFF`). On Ubuntu/Debian install with `sudo apt-get install pkg-config libgtk-3-dev`, then verify `pkg-config --modversion gtk+-3.0`. To use the portal backend instead, override `NFD_PORTAL=ON`.
- **Linux link error `ld.lld: error: unable to find library -lvdpau`**: install `libvdpau-dev` (`sudo apt-get install libvdpau-dev`). This library is used by FFmpeg VDPAU hardware-acceleration support and may be pulled transitively when statically linking OpenImageIO with FFmpeg enabled.
- **Qt6 static link errors mentioning `Brotli*` symbols**: rebuild `brotli` (or re-run `Qt6`) so the prefix has an `unofficial-brotli` CMake package shim.
- **OpenImageIO link errors mentioning `g_unicode_*` / `g_bytes_*` from `libharfbuzz.a`**: rebuild `harfbuzz` (and `freetype`) so HarfBuzz is built without GLib integration for static linking.
- **Missing optional repos**: `yaml-cpp`, `pystring`, `expat`, `pugixml`, `libxml2` are skipped if not present. On Windows, `libiconv` is expected via `external/vcpkg-export-libiconv.zip`.
- **OpenMP not found (macOS/Linux)**: set `OpenMP_ROOT` in `build.toml` or environment.
- **ASAN failures on Windows**: prefer clang-cl and ensure the MSVC AddressSanitizer component is installed.
- **PyOpenColorIO / PyOpenEXR link errors on Windows**: set `windows.msvc_runtime = "dynamic"` and `windows.python_wrappers = "on"` for wrapper builds.
- **Preflight only**: run `uv run build.py` (no args) to see tool/repo readiness without building.

## Notes

- The builder uses stamps in `./developer/_build/.stamps` (by default) to skip rebuilds when
  no repo/toolchain/flag changes are detected.
- Uncommitted working tree changes are not detected yet (use `--force` if needed).
- Optional repos (e.g., `yaml-cpp`, `pystring`, `pugixml`, `expat`) are skipped if missing.
