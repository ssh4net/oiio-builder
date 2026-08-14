"""Curated license policy for distributable dependency prefixes.

This is intentionally a build-selection policy, not a substitute for legal
advice or a complete software bill of materials.  It makes the licensing
choices that affect this repository's managed dependency graph explicit and
records them in every license-aware prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


NONGPL_STATIC = "nongpl-static"
LGPL_DYNAMIC = "lgpl-dynamic"


@dataclass(frozen=True)
class LicenseRecord:
    expression: str
    disposition: str
    note: str = ""


@dataclass(frozen=True)
class LicenseProfile:
    name: str
    linkage: str
    excluded_repositories: dict[str, str]
    allowed_dispositions: frozenset[str]
    consumer_compile_definitions: tuple[str, ...] = ()


# This covers every repository in build.toml.  Dual-license entries name the
# license selected by the profile.  Keep this table reviewed when adding or
# changing a repository/ref; an unknown entry is rejected by a profile.
LICENSE_RECORDS: dict[str, LicenseRecord] = {
    "zlib-ng": LicenseRecord("Zlib", "allow"),
    "pcre2": LicenseRecord("BSD-3-Clause", "allow"),
    "openssl": LicenseRecord("Apache-2.0", "allow"),
    "Qt6": LicenseRecord("LGPL-3.0-or-later OR GPL-3.0-only OR commercial", "lgpl", "The LGPL-3.0-or-later option is selected only for a shared Qt build; GPL-only modules and tools remain outside the profile."),
    "xz": LicenseRecord("0BSD for liblzma", "allow-with-constraint", "Only liblzma is installed; GPL command-line tools and scripts are disabled."),
    "libdeflate": LicenseRecord("MIT", "allow"),
    "zstd": LicenseRecord("BSD-3-Clause OR GPL-2.0-only", "allow-with-constraint", "The BSD-3-Clause option is selected."),
    "libiconv": LicenseRecord("LGPL-2.1-or-later", "lgpl"),
    "libxml2": LicenseRecord("MIT", "allow"),
    "glfw": LicenseRecord("Zlib", "allow"),
    "freeglut": LicenseRecord("MIT", "allow"),
    "glew": LicenseRecord("BSD-3-Clause", "allow"),
    "libjpeg-turbo": LicenseRecord("BSD-3-Clause", "allow"),
    "libpng": LicenseRecord("Libpng", "allow"),
    "libtiff": LicenseRecord("BSD-2-Clause", "allow"),
    "openjpeg": LicenseRecord("BSD-2-Clause", "allow"),
    "jasper": LicenseRecord("BSD-2-Clause", "allow"),
    "pugixml": LicenseRecord("MIT", "allow"),
    "nativefiledialog-extended": LicenseRecord("Zlib", "allow"),
    "giflib": LicenseRecord("MIT", "allow"),
    "libwebp": LicenseRecord("BSD-3-Clause", "allow"),
    "ptex": LicenseRecord("BSD-3-Clause", "allow"),
    "dng-sdk": LicenseRecord("Adobe DNG SDK / XMP permissive terms", "allow-with-constraint", "Retain the Adobe notices and record the exact supplied archive in distribution materials."),
    "LibRaw": LicenseRecord("CDDL-1.0 OR LGPL-2.1-or-later", "allow-with-constraint", "The CDDL-1.0 option is selected."),
    "libraw": LicenseRecord("CDDL-1.0 OR LGPL-2.1-or-later", "allow-with-constraint", "The CDDL-1.0 option is selected for the bundled LibRaw source."),
    "aom": LicenseRecord("BSD-2-Clause", "allow"),
    "libde265": LicenseRecord("LGPL-3.0-or-later", "lgpl"),
    "x265": LicenseRecord("GPL-2.0-only", "gpl"),
    "kvazaar": LicenseRecord("BSD-2-Clause", "allow"),
    "libheif": LicenseRecord("LGPL-3.0-or-later", "lgpl"),
    "ffmpeg": LicenseRecord("LGPL-2.1-or-later by default", "lgpl", "LGPL profiles force a shared build with GPL and nonfree parts disabled."),
    "libvpx": LicenseRecord("BSD-3-Clause", "allow"),
    "opus": LicenseRecord("BSD-3-Clause", "allow"),
    "libyuv": LicenseRecord("BSD-3-Clause", "allow"),
    "brotli": LicenseRecord("MIT", "allow"),
    "bzip2": LicenseRecord("BSD-style", "allow"),
    "sqlite": LicenseRecord("blessing / public domain", "allow"),
    "highway": LicenseRecord("Apache-2.0 OR BSD-3-Clause", "allow-with-constraint", "The Apache-2.0 option is selected."),
    "lcms2": LicenseRecord("MIT", "allow"),
    "freetype": LicenseRecord("FTL OR GPL-2.0-only", "allow-with-constraint", "The FreeType License option is selected."),
    "harfbuzz": LicenseRecord("MIT", "allow"),
    "eigen": LicenseRecord("MPL-2.0 with optional LGPL files", "allow-with-constraint", "EIGEN_MPL2_ONLY is exported to reject LGPL Eigen headers."),
    "LBFGSpp": LicenseRecord("MIT", "allow"),
    "imath": LicenseRecord("BSD-3-Clause", "allow"),
    "openjph": LicenseRecord("BSD-3-Clause", "allow"),
    "openexr": LicenseRecord("BSD-3-Clause", "allow"),
    "rapidobj": LicenseRecord("MIT", "allow"),
    "rapidfuzz-cpp": LicenseRecord("MIT", "allow"),
    "toml11": LicenseRecord("MIT", "allow"),
    "miniply": LicenseRecord("MIT", "allow"),
    "OpenMeta": LicenseRecord("MIT", "allow"),
    "expat": LicenseRecord("MIT", "allow"),
    "libjxl": LicenseRecord("BSD-3-Clause", "allow"),
    "libultrahdr": LicenseRecord("Apache-2.0 AND MIT", "allow"),
    "minizip-ng": LicenseRecord("Zlib", "allow"),
    "yaml-cpp": LicenseRecord("MIT", "allow"),
    "pystring": LicenseRecord("BSD-3-Clause", "allow"),
    "nanobind": LicenseRecord("BSD-3-Clause", "allow"),
    "pybind11": LicenseRecord("BSD-3-Clause", "allow"),
    "cpython": LicenseRecord("PSF-2.0", "allow"),
    "robinmap": LicenseRecord("MIT", "allow"),
    "fmt": LicenseRecord("MIT", "allow"),
    "spdlog": LicenseRecord("MIT", "allow"),
    "OpenColorIO": LicenseRecord("BSD-3-Clause", "allow"),
    "googletest": LicenseRecord("BSD-3-Clause", "allow"),
    "SPIRV-Headers": LicenseRecord("MIT", "allow"),
    "SPIRV-Tools": LicenseRecord("Apache-2.0", "allow"),
    "glslang": LicenseRecord("BSD-3-Clause / Apache-2.0 components", "allow-with-constraint", "Retain all upstream notices."),
    "imgui": LicenseRecord("MIT", "allow"),
    "imgui_test_engine": LicenseRecord("Dear ImGui Test Engine free/commercial terms", "caution", "Build/debug-only tooling; verify eligibility and do not treat it as a redistributable runtime dependency."),
    "OpenImageIO": LicenseRecord("Apache-2.0", "allow"),
}


_NONGPL_STATIC_EXCLUSIONS = {
    "Qt6": "Open-source static Qt may impose GPL obligations; the profile does not infer a commercial Qt entitlement.",
    "libiconv": "GNU libiconv libraries are LGPL-licensed.",
    "libde265": "libde265 is LGPL-3.0-or-later.",
    "x265": "x265 is GPL-2.0-only.",
    "libheif": "libheif is LGPL-3.0-or-later.",
    "ffmpeg": "FFmpeg's default build is LGPL and GPL-enabled configurations are also disallowed.",
}

_NONGPL_STATIC = LicenseProfile(
    name=NONGPL_STATIC,
    linkage="static",
    excluded_repositories=_NONGPL_STATIC_EXCLUSIONS,
    allowed_dispositions=frozenset({"allow", "allow-with-constraint", "caution"}),
    consumer_compile_definitions=("EIGEN_MPL2_ONLY",),
)

_LGPL_DYNAMIC_EXCLUSIONS = {
    "x265": "x265 is GPL-2.0-only and is outside every LGPL profile.",
}

_LGPL_DYNAMIC = LicenseProfile(
    name=LGPL_DYNAMIC,
    linkage="dynamic",
    excluded_repositories=_LGPL_DYNAMIC_EXCLUSIONS,
    allowed_dispositions=frozenset({"allow", "allow-with-constraint", "caution", "lgpl"}),
    consumer_compile_definitions=("EIGEN_MPL2_ONLY",),
)


# The builder defaults intentionally favor static libraries.  These final
# cache values reverse project-specific static defaults for a dynamic prefix.
# They are appended after repository defaults and user overrides.
_DYNAMIC_CMAKE_CACHE: dict[str, tuple[str, ...]] = {
    "aom": ("ENABLE_SHARED=ON",),
    "bzip2": ("ENABLE_SHARED_LIB=ON", "ENABLE_STATIC_LIB=OFF"),
    "freeglut": ("FREEGLUT_BUILD_STATIC_LIBS=OFF", "FREEGLUT_BUILD_SHARED_LIBS=ON"),
    "glew": ("glew-cmake_BUILD_SHARED=ON", "glew-cmake_BUILD_STATIC=OFF"),
    "highway": ("HWY_FORCE_STATIC_LIBS=OFF",),
    "imath": ("IMATH_BUILD_SHARED_LIBS=ON",),
    "jasper": ("JAS_ENABLE_SHARED=ON",),
    "kvazaar": ("BUILD_SHARED_LIBS=ON",),
    "lcms2": ("LCMS2_BUILD_SHARED=ON", "LCMS2_BUILD_STATIC=OFF"),
    "libdeflate": ("LIBDEFLATE_BUILD_STATIC_LIB=OFF", "LIBDEFLATE_BUILD_SHARED_LIB=ON"),
    "libjpeg-turbo": ("ENABLE_SHARED=ON", "ENABLE_STATIC=OFF"),
    "libjxl": ("JPEGXL_STATIC=OFF",),
    "libpng": ("PNG_SHARED=ON", "PNG_STATIC=OFF"),
    "libyuv": ("LIBYUV_BUILD_SHARED=ON",),
    "OpenImageIO": ("LINKSTATIC=OFF",),
    "openjpeg": ("BUILD_STATIC_LIBS=OFF", "BUILD_SHARED_LIBS=ON"),
    "opus": ("BUILD_SHARED_LIBS=ON",),
    "ptex": ("PTEX_BUILD_STATIC_LIBS=OFF", "PTEX_BUILD_SHARED_LIBS=ON"),
    "spdlog": ("SPDLOG_BUILD_SHARED=ON",),
    "xz": ("BUILD_SHARED_LIBS=ON",),
    "yaml-cpp": ("YAML_BUILD_SHARED_LIBS=ON",),
    "zstd": ("ZSTD_BUILD_SHARED=ON", "ZSTD_BUILD_STATIC=OFF", "ZSTD_PROGRAMS_LINK_SHARED=ON"),
}


# These selections can install GPL-licensed Qt artifacts even though the same
# Qt source repository also contains LGPL libraries.  A strict LGPL prefix
# must use a narrower submodule set or a separately reviewed commercial Qt.
_LGPL_DYNAMIC_REJECTED_QT6_MODULES = frozenset({"qtdeclarative", "qttools", "qtwayland"})

_LGPL_DYNAMIC_SHARED_ARTIFACTS: dict[str, dict[str, tuple[tuple[str, ...], ...]]] = {
    "Qt6": {
        "windows": (("bin/Qt6Core*.dll",),),
        "linux": (("lib/libQt6Core.so*", "lib64/libQt6Core.so*"),),
        "macos": (("lib/QtCore.framework/QtCore", "lib/libQt6Core*.dylib"),),
    },
    "libiconv": {
        "windows": (("bin/*iconv*.dll",), ("bin/*charset*.dll",)),
        "linux": (("lib/libiconv.so*", "lib64/libiconv.so*"),),
        "macos": (("lib/libiconv*.dylib",),),
    },
    "libde265": {
        "windows": (("bin/*de265*.dll",),),
        "linux": (("lib/libde265.so*", "lib64/libde265.so*"),),
        "macos": (("lib/libde265*.dylib",),),
    },
    "libheif": {
        "windows": (("bin/*heif*.dll",),),
        "linux": (("lib/libheif.so*", "lib64/libheif.so*"),),
        "macos": (("lib/libheif*.dylib",),),
    },
    "ffmpeg": {
        "windows": (
            ("bin/*avcodec*.dll",),
            ("bin/*avformat*.dll",),
            ("bin/*avutil*.dll",),
            ("bin/*swscale*.dll",),
        ),
        "linux": (
            ("lib/libavcodec.so*", "lib64/libavcodec.so*"),
            ("lib/libavformat.so*", "lib64/libavformat.so*"),
            ("lib/libavutil.so*", "lib64/libavutil.so*"),
            ("lib/libswscale.so*", "lib64/libswscale.so*"),
        ),
        "macos": (
            ("lib/libavcodec*.dylib",),
            ("lib/libavformat*.dylib",),
            ("lib/libavutil*.dylib",),
            ("lib/libswscale*.dylib",),
        ),
    },
}

_LGPL_DYNAMIC_FORBIDDEN_STATIC_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "Qt6": ("lib/libQt6Core*.a", "lib64/libQt6Core*.a"),
    "libiconv": ("lib/libiconv*.a", "lib64/libiconv*.a"),
    "libde265": ("lib/libde265*.a", "lib64/libde265*.a"),
    "libheif": ("lib/libheif*.a", "lib64/libheif*.a"),
    "ffmpeg": ("lib/libav*.a", "lib64/libav*.a", "lib/libswscale*.a", "lib64/libswscale*.a"),
}


def normalize_profile(value: object) -> str | None:
    """Return the canonical profile name, rejecting unimplemented profiles."""
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("_", "-")
    if normalized in {"", "none", "default", "unrestricted"}:
        return None
    aliases = {
        "nongpl": NONGPL_STATIC,
        "non-gpl": NONGPL_STATIC,
        "non-gpl-static": NONGPL_STATIC,
        "lgpl": LGPL_DYNAMIC,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized in {NONGPL_STATIC, LGPL_DYNAMIC}:
        return normalized
    if normalized in {"lgpl-static", "lgpl-mixed", "gpl-static", "gpl-dynamic", "gpl-mixed"}:
        raise ValueError(
            f"License profile {normalized!r} is documented but not implemented yet. "
            f"The currently supported license-aware profiles are {NONGPL_STATIC!r} and {LGPL_DYNAMIC!r}."
        )
    raise ValueError(
        f"Unknown license profile {value!r}. "
        f"Use {NONGPL_STATIC!r}, {LGPL_DYNAMIC!r}, or omit the profile for the existing unrestricted build."
    )


def resolve_profile(value: object) -> LicenseProfile | None:
    normalized = normalize_profile(value)
    if normalized is None:
        return None
    if normalized == NONGPL_STATIC:
        return _NONGPL_STATIC
    assert normalized == LGPL_DYNAMIC
    return _LGPL_DYNAMIC


def apply_profile_defaults(cfg: Any) -> None:
    """Apply non-negotiable build settings before recipes inspect the config."""
    profile = resolve_profile(getattr(cfg, "profile", None))
    if profile is None:
        return
    if profile.name == NONGPL_STATIC:
        cfg.static_default = True
        # These are GPL-family artifacts or support stacks only needed by the
        # rejected HEIF/FFmpeg feature paths.  Disabling them early also keeps
        # preflight from requesting prerequisites for excluded components.
        cfg.build_ffmpeg = False
        cfg.build_libheif = False
        cfg.build_libde265 = False
        cfg.build_x265 = False
        cfg.build_aom = False
        cfg.build_kvazaar = False
        cfg.build_qt6 = False
        if isinstance(getattr(cfg, "windows", None), dict):
            cfg.windows["build_ffmpeg"] = False
            cfg.windows["use_ffmpeg_from_prefix"] = False
    elif profile.name == LGPL_DYNAMIC:
        cfg.static_default = False
        cfg.build_x265 = False
        if isinstance(getattr(cfg, "windows", None), dict):
            cfg.windows["msvc_runtime"] = "dynamic"

        if bool(getattr(cfg, "build_qt6", False)):
            modules = {str(name).strip().lower() for name in getattr(cfg, "qt6_modules", ())}
            rejected = sorted(modules & _LGPL_DYNAMIC_REJECTED_QT6_MODULES)
            if rejected:
                raise ValueError(
                    "lgpl-dynamic rejects Qt submodules that install GPL-licensed artifacts: "
                    f"{', '.join(rejected)}. Select an LGPL-only Qt module set or use a "
                    "separately reviewed commercial Qt build."
                )


def rejected_reason(profile: LicenseProfile | None, repo_name: str) -> str | None:
    if profile is None:
        return None
    record = LICENSE_RECORDS.get(repo_name)
    if record is None:
        return "No reviewed license record exists for this repository."
    if record.disposition not in profile.allowed_dispositions:
        return profile.excluded_repositories.get(repo_name, f"{record.expression} is outside the profile.")
    return profile.excluded_repositories.get(repo_name)


def profile_cmake_args(profile: LicenseProfile | None, repo_name: str) -> list[str]:
    if profile is None:
        return []
    if profile.name == NONGPL_STATIC and repo_name == "xz":
        return [
            "-DXZ_NLS=OFF",
            "-DXZ_TOOL_XZ=OFF",
            "-DXZ_TOOL_XZDEC=OFF",
            "-DXZ_TOOL_LZMADEC=OFF",
            "-DXZ_TOOL_LZMAINFO=OFF",
            "-DXZ_TOOL_SCRIPTS=OFF",
            "-DXZ_DOC=OFF",
        ]
    if profile.name == NONGPL_STATIC and repo_name == "OpenImageIO":
        # Do not fall back to a system copy of an excluded dependency.
        return ["-DENABLE_FFMPEG=OFF", "-DENABLE_LIBHEIF=OFF"]
    if profile.name == LGPL_DYNAMIC:
        cache_values = ["BUILD_SHARED_LIBS=ON", "PKG_CONFIG_USE_STATIC_LIBS=OFF"]
        cache_values.extend(_DYNAMIC_CMAKE_CACHE.get(repo_name, ()))
        if repo_name == "libheif":
            # x265 is GPL and must not be rediscovered from the host system.
            cache_values.extend(("WITH_X265=OFF", "WITH_X265_PLUGIN=OFF"))
        return [f"-D{value}" for value in cache_values]
    return []


def profile_warnings(profile: LicenseProfile | None, repo_names: Iterable[str]) -> list[str]:
    if profile is None:
        return []
    names = set(repo_names)
    warnings = [
        "This is a curated build-selection policy, not legal advice or a complete SBOM.",
        "Retain notices and review patent, trademark, export-control, and system-library obligations separately.",
    ]
    if profile.name == LGPL_DYNAMIC:
        warnings.extend(
            [
                "LGPL libraries are shared endpoints; keep them replaceable and do not copy their code into proprietary binaries.",
                "Distributing LGPL binaries requires the applicable license texts, notices, exact corresponding source and modifications, and a compliance review.",
            ]
        )
        if "ffmpeg" in names:
            warnings.append("FFmpeg is forced shared with GPL/nonfree parts disabled; distribute its exact source and build configuration.")
        if "Qt6" in names:
            warnings.append("Qt is built shared under the LGPL option; verify every selected module and deployed plugin, and exclude GPL-only Qt artifacts.")
    if "eigen" in names:
        warnings.append("Eigen consumers must retain EIGEN_MPL2_ONLY; the installed Eigen3 CMake target exports it.")
    if "dng-sdk" in names:
        warnings.append("Adobe DNG/XMP: retain the supplied archive's notices and record its exact version/hash before redistribution.")
    if "imgui_test_engine" in names:
        warnings.append("Dear ImGui Test Engine is build/debug-only; verify its eligibility terms before any redistribution.")
    return warnings


def validate_installed_artifacts(
    profile: LicenseProfile | None,
    repo_name: str,
    install_prefix: Path,
    platform_os: str,
) -> None:
    """Reject an LGPL endpoint unless the installed artifact is replaceable."""
    if profile is None or profile.name != LGPL_DYNAMIC:
        return
    platform_groups = _LGPL_DYNAMIC_SHARED_ARTIFACTS.get(repo_name, {}).get(platform_os)
    if not platform_groups:
        return

    missing_groups: list[str] = []
    for alternatives in platform_groups:
        if any(any(install_prefix.glob(pattern)) for pattern in alternatives):
            continue
        missing_groups.append(" or ".join(alternatives))
    if missing_groups:
        raise RuntimeError(
            f"lgpl-dynamic requires shared {repo_name} artifacts under {install_prefix}; missing: "
            f"{', '.join(missing_groups)}"
        )

    if platform_os == "windows":
        # Windows import libraries use the same .lib extension as static
        # archives, so the DLL checks above are the enforceable distinction.
        return
    forbidden = [
        path
        for pattern in _LGPL_DYNAMIC_FORBIDDEN_STATIC_ARTIFACTS.get(repo_name, ())
        for path in install_prefix.glob(pattern)
    ]
    if forbidden:
        rendered = ", ".join(str(path) for path in sorted(forbidden))
        raise RuntimeError(f"lgpl-dynamic forbids static {repo_name} archives in the profile prefix: {rendered}")


def profile_manifest(
    profile: LicenseProfile,
    selected_repositories: Iterable[str],
    excluded_repositories: dict[str, str],
) -> dict[str, object]:
    repository_names = list(selected_repositories)
    selected: list[dict[str, str]] = []
    for name in sorted(repository_names, key=str.lower):
        record = LICENSE_RECORDS[name]
        entry = {
            "name": name,
            "license": record.expression,
            "disposition": record.disposition,
        }
        if record.note:
            entry["note"] = record.note
        selected.append(entry)

    excluded: list[dict[str, str]] = []
    for name in sorted(excluded_repositories, key=str.lower):
        record = LICENSE_RECORDS.get(name)
        entry = {"name": name, "reason": excluded_repositories[name]}
        if record is not None:
            entry["license"] = record.expression
        excluded.append(entry)

    return {
        "schema": 1,
        "kind": "resolved-license-policy",
        "profile": profile.name,
        "linkage": profile.linkage,
        "selected_repositories": selected,
        "excluded_repositories": excluded,
        "consumer_compile_definitions": list(profile.consumer_compile_definitions),
        "warnings": profile_warnings(profile, repository_names),
    }
