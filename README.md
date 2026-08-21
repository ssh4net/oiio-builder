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
- Perl 5 (OpenSSL configuration)
- OpenMP runtime when enabling OpenMP (for example: `libraw_enable_openmp="ON"`). MSVC uses the Visual Studio `vcomp` runtime; clang/clang-cl uses LLVM `libomp`.
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
   NASM_EXECUTABLE = "C:/Program Files/NASM/nasm.exe"
   PERL_EXECUTABLE = "C:/Strawberry/perl/bin/perl.exe"
   # Needed for clang-cl OpenMP; MSVC cl uses Visual Studio's vcomp runtime.
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
  - `by-build-type`: per-config prefixes (macOS/Linux: `Release/Debug/ASAN` subdirs under `install_prefix`; Windows: Debug+Release share `install_prefix`, ASAN uses `asan_prefix` or a derived path).
  - `suffix`: legacy Unix layout using `debug_suffix`/`asan_suffix`.
- `install_prefix`: canonical install prefix root (cross-platform).
- `asan_prefix`: optional explicit ASAN prefix (cross-platform).
- `profile`: optional license/linkage profile. `nongpl-static` rejects managed GPL/LGPL artifacts and forces static linkage. `lgpl-dynamic` rejects GPL artifacts, permits reviewed LGPL components only as shared libraries, and forces dynamic linkage plus the dynamic MSVC runtime.
- `profile_prefix_base`: root for isolated profile prefixes (default: `./developer/prefixes`).
- `prefix_base`: legacy fallback prefix root used when `install_prefix` is not set.
- `write_prefix_contract`: write and maintain a managed prefix contract bundle under `<prefix>/.oiio-builder/` (default: `true`).
- `build_types`: list of configs to build (`Debug`, `Release`, `ASAN`).
- `preferred_repo_order`: optional list of repo names that influences build order when multiple repos are ready (deps still win).
- `use_libcxx`: default on macOS/Linux; set `false` to use libstdc++.
- `use_ccache`: enable `ccache` compiler launcher on macOS/Linux (default: `true`). Disable with `--no-ccache` or `use_ccache=false`. Configure cache paths via `[global.env]` (`CCACHE_DIR`, `CCACHE_TEMPDIR`, `CCACHE_MAXSIZE`, …).
- `build_*` toggles: enable/disable stacks (GL, EXR, image IO, etc.).
- `build_cpython`: build CPython from source (`https://github.com/python/cpython.git`), enabled by default on all platforms.
- `sqlite` is built ahead of `cpython` when CPython is requested.
- `cpython_ref`: optional CPython git ref override (example: `3.13`, `v3.12.11`, commit SHA).
- `cpython_ref_type`: `branch` (default), `tag`, or `commit` for `cpython_ref`.
- `build_qt6`: build a minimal **static Qt6** stack into the prefix (for consumers like OpenImageIO `iv` and GPUpad).
- `build_dng_sdk`: build Adobe DNG SDK + XMP (via `DNG-CMake`) into the prefix (optional; disabled by default).
- `build_imgui`, `build_imgui_test_engine`: fetch optional Dear ImGui docking/test-engine sources (source-only repos; disabled by default).
- `build_libvpx`, `build_opus`, `build_libyuv`: build optional media dependencies for downstream RustDesk/RustAdmin/FFmpeg experiments (disabled by default).
- `build_nlohmann_json`: build/install the optional header-only nlohmann/json CMake package for local consumers (disabled by default).
  Consumers use `find_package(nlohmann_json CONFIG REQUIRED)` and link `nlohmann_json::nlohmann_json`.
- `build_toml11`: build/install optional header-only toml11 CMake package for local consumers (disabled by default).
- `windows.generator`: choose one of `msvc`, `ninja-msvc`, `msvc-clang-cl`, `ninja-clang-cl`.
- `windows.vs_generator`: optional CMake generator name override for `windows.generator=msvc`/`msvc-clang-cl` (e.g. `Visual Studio 18 2026` with CMake 4.2+).
- `windows.generator = "ninja-clang-cl"` prefers Visual Studio's bundled `clang-cl.exe`; use absolute `[global].cc` / `[global].cxx` paths to force a standalone LLVM install.
- `windows.build_ffmpeg`: defaults to `false`; when `true`, Windows builds use prebuilt FFmpeg by default, or native FFmpeg source build when run from MSYS2 (see below).
- `windows.use_ffmpeg_from_prefix`: defaults to `true`; on standard Windows builds, OpenImageIO auto-uses FFmpeg already installed in the active prefix even when `windows.build_ffmpeg = false`.
- `windows.msvc_runtime`: `static` (default, `/MT`/`/MTd`) or `dynamic` (`/MD`/`/MDd`).
- `windows.python_wrappers`: `auto` (default), `on`, `off` for OpenColorIO/OpenEXR/OpenMeta Python bindings.
  `auto` enables wrappers only when `windows.msvc_runtime=dynamic`.
- Windows Debug CPython remains a dynamic build (`python_d.exe` using `python3XY_d.dll`), and its extension modules remain dynamically loaded. OpenMeta installs `_openmeta_d...pyd` directly into the prefix for Debug builds, but defers wheel generation to Release because `uv` cannot create an isolated wheel environment from the Debug interpreter.
- `windows.cpython_fetch_externals`: `true` (default) uses `-e` (CPython fetch/builds externals); `false` passes `-E`.
- `sqlite` is source-built on every platform. Windows uses upstream `Makefile.msc` with `nmake`; macOS/Linux use upstream `Makefile.linux-generic` with GNU Make.
- On Windows, optional `libvpx` is imported from a vcpkg export zip (`external/vcpkg-export-libvpx.zip`) instead of source/autotools build.
- `windows.clangcl_extra_flags`: clang-cl x86_64 baseline extra flags (default if unset: `-msse4.1`).
- `windows.clangcl_extra_flags_append`: extra clang-cl x86_64 flags appended to the baseline (default: empty).
- `windows.env`: tool overrides for Windows (e.g. `PKG_CONFIG_EXECUTABLE`, `DOXYGEN_EXECUTABLE`).

Prefix precedence (all platforms):
- `global.install_prefix` / `global.asan_prefix`
- `global.prefix_base` (fallback when `install_prefix` is unset)

### Repo Defaults and Local Overrides

Repo graphs and global policy live in `build.toml`, but per-repo behavior lives under `builder/recipes/`.
Default CMake cache settings are tracked in `builder/recipes/defaults/<repo>.toml`; Python recipe modules
hold repo-specific hooks such as enable policy, dynamic CMake args, source patches, build-system selection,
environment adjustment, pre-build shims, post-install fixes, and stamp payload additions.

Local overrides are read from `build.user.toml` (gitignored) and merged on top of `build.toml`
(CLI flags still win). You can override `[global]`, `[windows]`, and per-repo CMake cache settings.

```toml
[global]
install_prefix = "./developer/install" # example

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
  - `install_prefix=/mnt/f/dev` → Release: `/mnt/f/dev/Release`, Debug: `/mnt/f/dev/Debug`, ASAN: `/mnt/f/dev/ASAN`
- macOS/Linux (`prefix_layout="suffix"`):
  - `install_prefix=/mnt/f/UBS` → Release: `/mnt/f/UBS`, Debug: `/mnt/f/UBSd`, ASAN: `/mnt/f/UBSasn`
- Windows:
  - Debug and Release share one prefix (debug builds first).
  - ASAN can use a separate prefix via `asan_prefix` (e.g., `./developer/asan`).
- License-aware profiles always use a separate root. For example, `--profile nongpl-static` writes `developer/prefixes/nongpl-static/Release` and `Debug` on macOS/Linux; Windows uses `developer/prefixes/nongpl-static` for Debug and Release.

## Install Markers (Prefix Retargeting)

The builder writes per-repo install markers under:

`<prefix>/.oiio-builder/install-stamps/<repo>/<build_type>.json`

If a repo is up-to-date but its marker is missing or mismatched (for example: you changed `install_prefix` or deleted/moved a
prefix directory), the builder automatically re-runs the repo install step instead of skipping it.
Use `--reinstall` / `--reinstall-all` to force reinstall even when markers are present.

## Prefix Contract Bundle

Each active install prefix can carry a managed contract bundle under:

`<prefix>/.oiio-builder/`

Files:
- `prefix-contract.json`: authoritative ABI/policy contract used by preflight.
- `prefix-init-cache.cmake`: safe shared cache defaults for `cmake -C`.
- `prefix-contract.cmake`: helper variables/functions for projects that want to opt into the contract from CMake.
- `prefix-presets.json`: hidden configure preset fragments that can be included from a source tree.
- `license-policy.json`: resolved repository license choices, exclusions, and warnings when a license-aware profile is active.

The contract is intended to protect a prefix from accidental reuse with incompatible settings such as:
- `libc++` vs `libstdc++`
- default static vs shared builds
- PIC on/off
- Windows CRT mode (`/MT` vs `/MD`)
- ASAN vs non-ASAN prefixes

Preflight reports contract state per computed prefix. A populated prefix without a matching contract is treated as an error.

## Common Commands

```bash
# Preflight checks (tools + repos)
uv run build.py --preflight

# List repos to build
uv run build.py --list-repos

# Clone/fetch/checkout repos only (no build)
uv run build.py --update-only

# Destructively reset and refresh every existing configured source checkout.
# This command must be used alone and requires typing FORCE-UPDATE.
uv run build.py --force-update

# Clone/fetch/checkout repos and run source-prep hooks only
uv run build.py --prepare-only
uv run build.py --prepare-only --only Qt6
uv run build.py --prepare-only --only OpenImageIO --apply-prefix-contract

# Print computed install prefixes
uv run build.py --print-prefixes

# Build the separately stamped GPL/LGPL-free static prefix
uv run build.py --profile nongpl-static --build-types Debug,Release
uv run build.py --profile lgpl-dynamic --build-types Debug,Release

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

`--prepare-only` is useful when a repo needs source hydration without starting a build. Current examples:
- Qt6 missing submodules after a branch/ref change or partial checkout (`init-repository` is run automatically).
- Repo-specific source prep hooks such as `glslang` external source staging when optimizer support is enabled.
- It also backfills the managed prefix contract bundle for the active prefixes.
- With `--apply-prefix-contract`, the builder also writes managed `CMakeUserPresets.json` shims into CMake source trees when no unmanaged file is present already.

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
:: Ninja + MSVC (set windows.generator = "ninja-msvc")
:: Run from a Visual Studio Developer Prompt/PowerShell so cl.exe is on PATH.
uv run build.py --build-types Debug,Release

:: Ninja + clang-cl
:: (set windows.generator = "ninja-clang-cl")
uv run build.py --config build.toml --build-types Debug,Release

:: Visual Studio solution + clang-cl
:: (set windows.generator = \"msvc-clang-cl\" in build.toml)
uv run build.py --build-types Debug,Release
```

### Windows: MSYS2 bootstrap
If you want FFmpeg source builds or any Windows autotools repo, install MSYS2 from the official distribution:

- https://www.msys2.org/

Start an MSYS2 shell on Windows (UCRT64 is fine) and install the minimum POSIX tools this builder expects:

```bash
pacman -S --needed base-devel unzip git
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
```

If your MSYS profile still does not expose `cmp` or `make`, install them explicitly too:

```bash
pacman -S --needed diffutils make
```

`base-devel`/`diffutils`/`make` are required for FFmpeg/autotools source builds because they provide core MSYS tools such as `cmp` and `make`.
MSVC CMake builds still need a native Windows `cmake.exe` and `ninja.exe` from CMake, Visual Studio, or another Windows-native install; do not let `C:/msys64/usr/bin/ninja.exe` be the Ninja used by `windows.generator = "ninja-msvc"`.

Enable Windows FFmpeg source builds in your local `build.user.toml`:

```toml
[windows]
build_ffmpeg = true
```

Then verify the shell is usable for this repo:

```bash
uv run build.py --preflight
```

For FFmpeg source builds, preflight should report:

```text
FFmpeg (Windows mode):
  source build: enabled (MSYS2 environment detected)
  shell: ok (...)
  make: ok (...)
```

### Windows: FFmpeg
By default, `windows.build_ffmpeg = false`, but `windows.use_ffmpeg_from_prefix = true`.
That means standard Windows builds skip the FFmpeg repo step and let OpenImageIO use an MSVC-built static FFmpeg already installed in the active prefix.

When `windows.build_ffmpeg = true`, the builder picks one of two modes:
1. **MSYS2 source-build mode** (auto): if `MSYSTEM`/MSYS2 is detected, FFmpeg is built from source via `bash + make` with `--toolchain=msvc`.
2. **Prebuilt mode** (fallback): if MSYS2 is not detected, install/copy an **MSVC-built static** FFmpeg into the same prefix used by this script (headers under `<prefix>/include`, libs under `<prefix>/lib`).

Notes:
- With `windows.build_ffmpeg = false`, only the prefix probe is used; the FFmpeg repo is not built.
- For `windows.generator = "ninja-msvc"`, the builder explicitly configures `cl` / `cl`.
- For `windows.generator = "ninja-clang-cl"` / `msvc-clang-cl`, FFmpeg is configured with `clang-cl`; Ninja clang-cl prefers the Visual Studio bundled LLVM toolset before PATH.
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

### SQLite

SQLite is built from its canonical source checkout in every linkage and license profile. The recipe enables JSON, FTS5 with the built-in `unicode61` tokenizer, RTree, Geopoly, and zlib-backed ZIP/SQLAR support. ICU and external Tcl are not required.

- Windows uses upstream `Makefile.msc` with `nmake` in a separate build directory. Run from a Visual Studio Native Tools prompt/PowerShell, or let the builder discover and load the VS 2022 environment.
- macOS/Linux use upstream `Makefile.linux-generic` in a separate build tree with explicit FTS5, RTree, Geopoly, and linkage options. This avoids relying on a generated configure wrapper from a moving SQLite checkout.
- Static prefixes install the SQLite static library and `sqlite3_zipfile` static extension library. Register `sqlite3_zipfile_init` in the consuming application before using the SQL `zipfile` virtual table.
- Dynamic prefixes install the SQLite shared library plus a loadable `zipfile` module. Release modules can be loaded by filename; Debug Windows modules use a `d` postfix and should specify the `sqlite3_zipfile_init` entry point explicitly.
- The recipe installs `SQLite3Config.cmake`, providing `SQLite::SQLite3`, compatibility target `SQLite3::SQLite3`, and `SQLite::Zipfile`, plus `sqlite3.pc`.

Focused builds:

```bat
uv run build.py --only sqlite --build-types Debug,Release
uv run build.py --profile nongpl-static --only sqlite --build-types Debug,Release
uv run build.py --profile lgpl-dynamic --only sqlite --build-types Debug,Release
```

### Qt6 (static, optional)

Enable Qt6 builds by setting `build_qt6 = true` (recommended: in `build.user.toml`):
```toml
[global]
build_qt6 = true
```

Default Qt module set (static):
- `qtbase`, `qttools`. This covers OpenImageIO `iv` and the Qt Designer app while avoiding the QML/Quick/Multimedia stack.

If you only want the smallest OpenImageIO-only Qt build, override it locally:
```toml
[global]
build_qt6 = true
qt6_modules = ["qtbase"]
```

If you want the larger gpupad-oriented Qt stack, override it locally:
```toml
[global]
build_qt6 = true
qt6_modules = ["qtbase", "qttools", "qtdeclarative", "qtshadertools", "qtmultimedia"]
```

To request a broader Qt stack, set `qt6_modules` explicitly in `build.user.toml`:
```toml
[global]
build_qt6 = true
qt6_modules = [
  "qtbase",
  "qttools",
  "qtdeclarative",
  "qtshadertools",
  "qtmultimedia",
  "qtimageformats",
  "qtsvg",
  "qtwayland", # Linux only
]
```

Notes:
- `qttools` is part of the default set. The builder disables Qt's Clang-backed tooling features (`clangcpp`/`qdoc`) to avoid distro-specific `lupdate` link failures against system LLVM/Clang packages while still building Designer.
- On Linux with `use_libcxx = true`, the builder also disables Qt ICU support. Distro ICU static archives are commonly built against `libstdc++`, which breaks static Qt consumers linked with `libc++`.
- `qtwayland` is Linux-only and is not included unless explicitly listed.
- System FreeType and system HarfBuzz are used for the default Qt build.

Build only Qt6:
```bash
uv run build.py --build-types Debug,Release --only Qt6
```

Skip Qt6 (build everything else):
```bash
uv run build.py --build-types Debug,Release --skip Qt6
```

OpenSSL is built from the upstream `openssl-4.0` branch without optional zlib
or ICU dependencies. Build it independently with:

```bash
uv run build.py --build-types Debug,Release --only openssl
```

Windows requirements:
- Use the native Windows builder from a Visual Studio 2022 Native Tools environment.
- Install Strawberry Perl and NASM, then put both on `PATH` or set
  `PERL_EXECUTABLE` and `NASM_EXECUTABLE` under `[windows.env]`.
- `nmake.exe`, `cl.exe`, `lib.exe`, `link.exe`, `rc.exe`, and `mt.exe` come
  from the Visual Studio C++ workload and Windows SDK.
- Debug and Release share the managed Windows prefix. Debug OpenSSL keeps its
  configuration and provider modules under `<prefix>/Debug` and exposes
  `libcryptod.lib`/`libssld.lib` compatibility names in `<prefix>/lib`.

WSL/Linux requirements are `build-essential`, Perl, Make, and NASM on x86-64.
A WSL run produces Linux libraries; it cannot produce the MSVC DLL/import-lib
package. OpenSSL recommends an ext4 source/build tree rather than `/mnt/*` for
speed and to avoid NTFS/WSL permission or line-ending failures.

Linux notes
- The default Qt build uses XCB (`-qpa xcb`).
- If you include `qtwayland` in `qt6_modules`, the builder switches to `-qpa xcb;wayland` and requires the relevant Wayland development packages plus `wayland-scanner` in `PATH`.

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
NASM_EXECUTABLE = "C:\\Program Files\\NASM\\nasm.exe"
PERL_EXECUTABLE = "C:\\Strawberry\\perl\\bin\\perl.exe"
```

## Troubleshooting

- **Rebuild not triggered after local edits**: stamps track dependency fingerprints and applied per-repo option layers, but not uncommitted working tree changes. Use `--force --only <repo>` for targeted rebuilds or `--force-all` for a clean run.
- **A source checkout blocks `--update`**: normal updates intentionally preserve tracked changes and local commits. Run `uv run build.py --force-update` by itself, review its warning, and type `FORCE-UPDATE` to restore every existing configured checkout. It discards tracked changes/local commits and ordinary untracked files, but leaves ignored files and does not clone missing sources. A path that Git cannot remove is reported as an incomplete checkout while the remaining refreshes continue.
- **uv cache permission issues**: set `UV_CACHE_DIR` to a writable directory (e.g. `UV_CACHE_DIR=/tmp/uv-cache`).
- **nativefiledialog-extended (Linux) missing/broken GTK deps**: the builder configures `nativefiledialog-extended` with the GTK3 backend (`NFD_PORTAL=OFF`). On Ubuntu/Debian install with `sudo apt-get install pkg-config libgtk-3-dev`, then verify `pkg-config --modversion gtk+-3.0`. To use the portal backend instead, override `NFD_PORTAL=ON`.
- **Linux link error `ld.lld: error: unable to find library -lvdpau`**: install `libvdpau-dev` (`sudo apt-get install libvdpau-dev`). This library is used by FFmpeg VDPAU hardware-acceleration support and may be pulled transitively when statically linking OpenImageIO with FFmpeg enabled.
- **Linux link error `ld.lld: error: unable to find library -lsystemd`**: install `libsystemd-dev` (`sudo apt-get install libsystemd-dev`). This library is pulled in by static Qt6 DBus linkage when linking the OpenImageIO `iv` app.
- **OpenImageIO/Qt6 link errors mentioning `std::condition_variable`, `std::__once_*`, or `std::__throw_system_error` from `libicuuc.a` / `libicui18n.a`**: rebuild Qt6 after this builder change so Qt is configured with `FEATURE_icu=OFF` for Linux `libc++` builds.
- **Qt6 static link errors mentioning `Brotli*` symbols**: rebuild `brotli` (or re-run `Qt6`) so the prefix has an `unofficial-brotli` CMake package shim.
- **OpenImageIO link errors mentioning `g_unicode_*` / `g_bytes_*` from `libharfbuzz.a`**: rebuild `harfbuzz` (and `freetype`) so HarfBuzz is built without GLib integration for static linking.
- **Missing optional repos**: `yaml-cpp`, `pystring`, `expat`, `pugixml`, `libxml2` are skipped if not present. On Windows, `libiconv` is expected via `external/vcpkg-export-libiconv.zip`.
- **OpenMP not found (macOS/Linux)**: set `OpenMP_ROOT` in `build.toml` or environment.
- **NASM not detected on Windows**: set `windows.env.NASM_EXECUTABLE = "C:/Program Files/NASM/nasm.exe"` in `build.user.toml`. The builder also probes the default NASM installer path automatically.
- **OpenSSL cannot find Perl or `nmake` on Windows**: install Strawberry Perl, set `windows.env.PERL_EXECUTABLE` if it is not on `PATH`, and run the builder from a Visual Studio Native Tools prompt. WSL Perl is valid only for a Linux/WSL OpenSSL build.
- **Windows Ninja build unexpectedly tries `clang-cl`**: with `windows.generator = "ninja-msvc"` the builder now pins `cl` explicitly. If preflight still reports `cc: missing (cl)`, launch the build from a Visual Studio Developer Prompt/PowerShell.
- **Windows CMake try-compile fails with `/bin/sh: ... cl.EXE: command not found`**: CMake picked MSYS2 POSIX Ninja (`C:/msys64/usr/bin/ninja.exe`). Put a native Ninja from CMake or Visual Studio earlier in `PATH`, set `windows.env.CMAKE_MAKE_PROGRAM = "C:/Program Files/CMake/bin/ninja.exe"`, or switch to `windows.generator = "msvc"`.
- **Windows CMake try-compile fails with `rc` or `CMAKE_MT-NOTFOUND`**: install the Windows 10/11 SDK via the Visual Studio C++ workload. The builder now probes `rc.exe` and `mt.exe` and reports them in preflight.
- **SQLite reports missing `nmake.exe`, `link.exe`, or (for static builds) `lib.exe`**: use a Visual Studio Native Tools prompt/PowerShell or install the Visual Studio C++ workload. SQLite's Windows recipe uses the upstream MSVC-compatible `Makefile.msc`, independently of the CMake generator selected for other repositories.
- **SQLite's POSIX generator reports an invalid Tcl command containing the amalgamation header**: the source checkout was converted to CRLF. Set `core.autocrlf=false` for the builder's WSL/macOS/Linux source store, then run the guarded `--force-update` operation for `sqlite`; the recipe now detects this before starting compilation.
- **ASAN failures on Windows**: prefer clang-cl and ensure the MSVC AddressSanitizer component is installed.
- **PyOpenColorIO / PyOpenEXR link errors on Windows**: set `windows.msvc_runtime = "dynamic"` and `windows.python_wrappers = "on"` for wrapper builds.
- **Preflight only**: run `uv run build.py` (no args) to see tool/repo readiness without building.

## Notes

- The builder uses stamps under the effective host build root (for example,
  `./developer/_build/linux/.stamps` or `./developer/_build/windows/.stamps`) to skip rebuilds when
  no repo/toolchain/flag changes are detected.
- Build trees are host-specific under the configured `build_root`. For example, a base `developer/_build`
  becomes `developer/_build/linux` under WSL/Linux and `developer/_build/windows` under native Windows.
  This avoids CMake cache, log, and stamp collisions when the same checkout is used from both hosts.
- Uncommitted working tree changes are not detected yet (use `--force` if needed).
- Optional repos (e.g., `yaml-cpp`, `pystring`, `pugixml`, `expat`) are skipped if missing.
