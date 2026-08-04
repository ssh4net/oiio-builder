"""Curated license policy for distributable dependency prefixes.

This is intentionally a build-selection policy, not a substitute for legal
advice or a complete software bill of materials.  It makes the licensing
choices that affect this repository's managed dependency graph explicit and
records them in every license-aware prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


NONGPL_STATIC = "nongpl-static"


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
    consumer_compile_definitions: tuple[str, ...] = ()


# This covers every repository in build.toml.  Dual-license entries name the
# license selected by the profile.  Keep this table reviewed when adding or
# changing a repository/ref; an unknown entry is rejected by a profile.
LICENSE_RECORDS: dict[str, LicenseRecord] = {
    "zlib-ng": LicenseRecord("Zlib", "allow"),
    "pcre2": LicenseRecord("BSD-3-Clause", "allow"),
    "openssl": LicenseRecord("Apache-2.0", "allow"),
    "Qt6": LicenseRecord("LGPL-3.0-or-later OR GPL-3.0-only OR commercial", "reject", "Open-source static Qt is outside this profile; use a separately reviewed commercial Qt setup if needed."),
    "xz": LicenseRecord("0BSD for liblzma", "allow-with-constraint", "Only liblzma is installed; GPL command-line tools and scripts are disabled."),
    "libdeflate": LicenseRecord("MIT", "allow"),
    "zstd": LicenseRecord("BSD-3-Clause OR GPL-2.0-only", "allow-with-constraint", "The BSD-3-Clause option is selected."),
    "libiconv": LicenseRecord("LGPL-2.1-or-later", "reject"),
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
    "libde265": LicenseRecord("LGPL-3.0-or-later", "reject"),
    "x265": LicenseRecord("GPL-2.0-only", "reject"),
    "kvazaar": LicenseRecord("BSD-2-Clause", "allow"),
    "libheif": LicenseRecord("LGPL-3.0-or-later", "reject"),
    "ffmpeg": LicenseRecord("LGPL-2.1-or-later by default", "reject", "The profile rejects the LGPL build as well as GPL-enabled FFmpeg."),
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
    consumer_compile_definitions=("EIGEN_MPL2_ONLY",),
)


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
    }
    normalized = aliases.get(normalized, normalized)
    if normalized == NONGPL_STATIC:
        return normalized
    if normalized in {"lgpl-static", "lgpl-dynamic", "lgpl-mixed", "gpl-static", "gpl-dynamic", "gpl-mixed"}:
        raise ValueError(
            f"License profile {normalized!r} is documented but not implemented yet. "
            f"The currently supported license-aware profile is {NONGPL_STATIC!r}."
        )
    raise ValueError(
        f"Unknown license profile {value!r}. "
        f"Use {NONGPL_STATIC!r} or omit the profile for the existing unrestricted build."
    )


def resolve_profile(value: object) -> LicenseProfile | None:
    normalized = normalize_profile(value)
    if normalized is None:
        return None
    assert normalized == NONGPL_STATIC
    return _NONGPL_STATIC


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


def rejected_reason(profile: LicenseProfile | None, repo_name: str) -> str | None:
    if profile is None:
        return None
    record = LICENSE_RECORDS.get(repo_name)
    if record is None:
        return "No reviewed license record exists for this repository."
    if record.disposition == "reject":
        return profile.excluded_repositories.get(repo_name, f"{record.expression} is outside the profile.")
    return profile.excluded_repositories.get(repo_name)


def profile_cmake_args(profile: LicenseProfile | None, repo_name: str) -> list[str]:
    if profile is None or profile.name != NONGPL_STATIC:
        return []
    if repo_name == "xz":
        return [
            "-DXZ_NLS=OFF",
            "-DXZ_TOOL_XZ=OFF",
            "-DXZ_TOOL_XZDEC=OFF",
            "-DXZ_TOOL_LZMADEC=OFF",
            "-DXZ_TOOL_LZMAINFO=OFF",
            "-DXZ_TOOL_SCRIPTS=OFF",
            "-DXZ_DOC=OFF",
        ]
    if repo_name == "OpenImageIO":
        # Do not fall back to a system copy of an excluded dependency.
        return ["-DENABLE_FFMPEG=OFF", "-DENABLE_LIBHEIF=OFF"]
    return []


def profile_warnings(profile: LicenseProfile | None, repo_names: Iterable[str]) -> list[str]:
    if profile is None:
        return []
    names = set(repo_names)
    warnings = [
        "This is a curated build-selection policy, not legal advice or a complete SBOM.",
        "Retain notices and review patent, trademark, export-control, and system-library obligations separately.",
    ]
    if "eigen" in names:
        warnings.append("Eigen consumers must retain EIGEN_MPL2_ONLY; the installed Eigen3 CMake target exports it.")
    if "dng-sdk" in names:
        warnings.append("Adobe DNG/XMP: retain the supplied archive's notices and record its exact version/hash before redistribution.")
    if "imgui_test_engine" in names:
        warnings.append("Dear ImGui Test Engine is build/debug-only; verify its eligibility terms before any redistribution.")
    return warnings


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
