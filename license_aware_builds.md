# License-Aware Build Profiles

This document records the builder's licensing profile design. It is a
technical distribution policy, not legal advice. A completed prefix still
needs a release-specific review of notices, source offers, patents,
trademarks, export controls, system libraries, and the exact product
distribution method.

## Names and Intent

The canonical profile families are `nongpl`, `lgpl`, and `gpl`. Linkage
modifiers are `static`, `dynamic`, and `mixed`.

| Profile | Intended policy | Status |
| --- | --- | --- |
| `nongpl-static` | Reject GPL-family artifacts from the managed prefix. Static, PIC, intended for proprietary projects that do not need HEIF or FFmpeg. | Implemented |
| `lgpl-dynamic` | Reject GPL; permit LGPL only through normal shared-library distribution with source/notices and LGPL compliance review. | Implemented |
| `lgpl-mixed` | Reject GPL; static libraries may be contained only in dynamically replaceable LGPL/open-source endpoints. | Planned |
| `gpl-static`, `gpl-dynamic`, `gpl-mixed` | Permit GPL only for a GPL-compatible open-source product; flag incompatible licenses. | Planned |

The CLI accepts `nongpl-static` and `lgpl-dynamic`. Other planned names are
deliberately rejected rather than silently producing an unrestricted prefix.

## Using `nongpl-static`

```bash
# Read-only validation of the resolved profile
uv run build.py --profile nongpl-static --preflight
uv run build.py --profile nongpl-static --dry-run --build-types Release

# Build the usable prefixes
uv run build.py --profile nongpl-static --build-types Debug,Release
```

The profile never reuses the normal `install_prefix`. Its default locations
are:

```text
developer/prefixes/nongpl-static/Release
developer/prefixes/nongpl-static/Debug
developer/prefixes/nongpl-static/ASAN
```

On Windows, Debug and Release share `developer/prefixes/nongpl-static`, in
line with the existing Windows prefix policy. Set `profile_prefix_base` in
`build.user.toml` to change the parent directory:

```toml
[global]
profile = "nongpl-static"
profile_prefix_base = "E:/third-party-prefixes"
```

Every profile prefix receives these files under `.oiio-builder/`:

- `prefix-contract.json`, which makes the license profile a hard compatibility
  property of the prefix;
- `license-policy.json`, the resolved repositories, dual-license choices,
  rejected repositories, consumer compile definitions, and warnings; and
- the normal CMake contract/preset files and build stamps, which include the
  active profile in their fingerprints.

The manifest is a traceability record of the builder's reviewed policy; it is
not an SPDX SBOM or a legal conclusion.

## Current `nongpl-static` Rules

The profile forces static builds and rejects these managed repositories:

| Repository | Reason |
| --- | --- |
| `x265` | GPL-2.0-only |
| `libheif`, `libde265` | LGPL-3.0-or-later |
| `ffmpeg` | Default FFmpeg is LGPL; GPL-enabled configurations are also disallowed |
| `libiconv` | LGPL-2.1-or-later |
| `Qt6` | The builder's open-source static Qt path cannot assume a commercial Qt entitlement |

It also disables the HEIF-only `aom` and `kvazaar` build paths, preventing an
unneeded codec stack from being built. OpenImageIO is configured with
`ENABLE_FFMPEG=OFF` and `ENABLE_LIBHEIF=OFF` after user CMake overrides, so it
cannot fall back to a system copy of either rejected dependency.

Qt has module- and tool-specific licensing variations, including some GPL
exceptions, but those are not a blanket permission to statically embed all Qt
open-source components in proprietary software. `nongpl-static` therefore
does not make that assumption; a commercial Qt build belongs in a separately
reviewed profile or prefix.

The following dual or mixed cases are selected deliberately:

| Component | Profile choice / enforcement |
| --- | --- |
| Eigen | MPL-2.0-only path. `EIGEN_MPL2_ONLY` is compiled into the build and exported by `Eigen3::Eigen`, so an LGPL Eigen header causes a compile error. Direct header users must preserve this definition. |
| XZ | `liblzma` only. CMake disables the XZ tools, scripts, NLS, and documentation so GPL command-line artifacts are not installed. The autotools route already disables those tools. |
| zstd | BSD-3-Clause option, not its GPL alternative. |
| FreeType | FreeType License option, not GPL. |
| LibRaw | CDDL-1.0 option, not LGPL. |
| Highway | Apache-2.0 option. |
| Adobe DNG/XMP | Permissive vendor terms as supplied by the archive; retain its notices and record the archive version/hash. |
| Dear ImGui Test Engine | Build/debug-only custom terms. It is not a final runtime dependency; verify free-license eligibility before any redistribution. |

All remaining repositories in `build.toml` have an explicit reviewed entry in
`builder/license_policy.py`. Adding or changing a repository/ref requires
reviewing and updating that record; a profile refuses an unrecorded repository.

`nongpl-static` means that the builder excludes its known managed GPL/LGPL
artifacts. It does not prove that every downstream executable, manually added
archive, system dependency, or product asset is proprietary-safe. Run
`verify_static_prefix.sh <prefix>` after building, inspect link maps and
installed artifacts, and keep release notices with the shipped product.

## Using `lgpl-dynamic`

```bash
uv run build.py --profile lgpl-dynamic --preflight
uv run build.py --profile lgpl-dynamic --build-types Debug,Release
```

This profile forces `BUILD_SHARED_LIBS=ON`, dynamic pkg-config discovery, and
shared variants for repositories whose own cache options otherwise override
`BUILD_SHARED_LIBS`. On Windows it also forces the dynamic MSVC runtime. GPL
repositories are rejected from the graph; currently this excludes `x265`.

The LGPL-specific guards are non-negotiable and are appended after local CMake
overrides:

- FFmpeg builds shared libraries with GPL and nonfree parts disabled. A
  Windows FFmpeg import library is rejected unless matching DLLs exist in the
  profile prefix.
- libheif builds shared and cannot enable or rediscover x265. Its libde265,
  AOM, and Kvazaar paths remain available under their reviewed licenses.
- Windows libiconv imports must contain the libiconv and libcharset DLLs.
- Qt builds shared. Because a single Qt source repository contains both LGPL
  libraries and GPL-only artifacts, `qtdeclarative`, `qttools`, and
  `qtwayland` are rejected by this strict profile. Use a narrower LGPL-only
  module set or a separately reviewed commercial Qt build.

The generated manifest warns that dynamic linking is only the linkage part of
LGPL compliance. A distributor still needs the applicable license text and
notices, exact corresponding source and modifications, replacement ability,
and a release-specific compliance review. FFmpeg and Qt each require their
own exact component and deployment review.

## LGPL Mixed-Mode Design

LGPL does not mean proprietary application source must automatically be
published. The obligations depend on the LGPL version, the linking model, and
how the product is conveyed. Dynamic linking is usually simpler, but shipping
a shared LGPL library still requires the relevant LGPL source/notices.

For static LGPL linking, users generally need a practical way to modify the
LGPL component and relink the combined work. Therefore the intended
`lgpl-mixed` design is not “static-link LGPL directly into the proprietary
executable.” It is an open-source, dynamically replaceable endpoint:

```text
Proprietary application
  -> OpenImageIO shared library
       -> heif.imageio plugin: static libheif + permitted codec archives
       -> ffmpeg.imageio plugin: static LGPL-only FFmpeg archives
```

The application links only to OpenImageIO's shared import library. Each
LGPL-bearing plugin is a separate DLL/shared object that can be rebuilt and
replaced. OIIO must use `EMBEDPLUGINS=OFF`; the LGPL archives must never be
linked into the final proprietary executable.

`lgpl-mixed` needs a generated compliance kit per endpoint:

- exact source and modifications for the LGPL component;
- exact OIIO/plugin source or relinkable objects, plus build scripts, flags,
  patches, versions, and toolchain details sufficient to build a compatible
  replacement;
- license texts and notices;
- any required reverse-engineering permission for debugging modifications;
- for LGPLv3 where applicable, installation/replacement information; and
- no signing, installer, loader, or runtime restriction that prevents use of
  a compatible rebuilt endpoint.

Before making it the default, compare `direct-shared`, an aggregated OIIO
shared library, and separate OIIO plugins for binary size, startup behavior,
feature isolation, and replacement tests. The plugin layout is the leading
design because it makes optional codecs and their compliance packages
separable.

For any LGPL mode, FFmpeg must remain LGPL-only (`--disable-gpl` and no
GPL-only external components), and x265 remains excluded. HEIF codec choices
also need a separate patent and distribution review; copyright license choice
alone is not enough.

## Planned Implementation Sequence

1. Build and use `nongpl-static`; validate its static artifacts and generated
   policy manifest in real consumer projects.
2. Add an artifact-level notice/SBOM generator and release checklist.
3. Prototype OIIO non-embedded image-I/O plugins and compare the three LGPL
   endpoint layouts.
4. Extend `lgpl-dynamic` compliance artifact generation, then add
   `lgpl-mixed` only with generated source/relink kits and replacement smoke
   tests.
5. Add GPL profiles with explicit downstream project-license confirmation and
   compatibility warnings.
