from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil


STAMP_REVISION = "10"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return bool(getattr(cfg, "build_dng_sdk", False))


@dataclass(frozen=True)
class _VendorStamp:
    path: str
    size: int
    mtime: int


def _read_vendor_stamp(path: Path) -> _VendorStamp | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < 3:
        return None
    try:
        size = int(lines[1].strip())
        mtime = int(lines[2].strip())
    except ValueError:
        return None
    return _VendorStamp(path=lines[0].strip(), size=size, mtime=mtime)


def _write_vendor_stamp(path: Path, archive: Path) -> None:
    try:
        st = archive.stat()
    except OSError:
        return
    text = f"{archive}\n{st.st_size}\n{int(st.st_mtime)}\n"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        return


def _resolve_dng_sdk_archive(builder) -> Path | None:
    cfg = builder.config.global_cfg
    external_dir = cfg.repo_root / "external"

    override = (
        cfg.env.get("DNGSDK_ARCHIVE")
        or cfg.env.get("DNG_SDK_ARCHIVE")
        or os.environ.get("DNGSDK_ARCHIVE")
        or os.environ.get("DNG_SDK_ARCHIVE")
    )
    if override:
        value = Path(os.path.expandvars(override)).expanduser()
        if not value.is_absolute():
            value = (cfg.repo_root / value).resolve()
        return value

    if not external_dir.is_dir():
        return None

    preferred = [
        external_dir / "dng_sdk_1_7_1_0.zip",
        external_dir / "dng_sdk_1_7_1_0.tar.gz",
        external_dir / "dng_sdk_1_7_1_0.tgz",
    ]
    for candidate in preferred:
        if candidate.exists():
            return candidate

    patterns = [
        "dng_sdk*.zip",
        "dng_sdk*.tar.gz",
        "dng_sdk*.tgz",
        "*dng*sdk*.zip",
        "*DNG*SDK*.zip",
    ]
    matches: list[Path] = []
    for pat in patterns:
        matches.extend(sorted(external_dir.glob(pat)))
    if matches:
        return matches[0]
    return None


def _find_dng_sdk_dir(root: Path) -> Path | None:
    # Expect: dng_sdk/source/dng_host.h
    candidates = list(root.rglob("dng_host.h"))
    for candidate in candidates:
        if candidate.parent.name != "source":
            continue
        if candidate.parent.parent.name != "dng_sdk":
            continue
        return candidate.parent.parent
    return None


def _find_xmp_dir(root: Path) -> Path | None:
    """Locate the XMP payload root.

    The Adobe DNG SDK commonly ships XMP sources as:
      - xmp/toolkit/public/include/XMP.hpp

    Some layouts may be:
      - xmp/public/include/XMP.hpp

    DNG-CMake expects to build sources under xmp/toolkit/, so patch_source will
    normalize the staging destination accordingly.
    """

    fallback_toolkit: Path | None = None
    for candidate in root.rglob("XMP.hpp"):
        if candidate.parent.name != "include":
            continue
        if candidate.parent.parent.name != "public":
            continue

        for parent in candidate.parents:
            if parent.name == "xmp":
                return parent
            if fallback_toolkit is None and parent.name == "toolkit":
                fallback_toolkit = parent
    return fallback_toolkit


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return

    dng_expected = src_dir / "dng_sdk" / "source" / "dng_host.h"
    xmp_expected_header = src_dir / "xmp" / "toolkit" / "public" / "include" / "XMP.hpp"
    xmp_expected_source = src_dir / "xmp" / "toolkit" / "XMPCore" / "source" / "WXMPDocOps.cpp"
    have_sources = dng_expected.exists() and xmp_expected_header.exists() and xmp_expected_source.exists()

    if not have_sources:
        archive_or_dir = _resolve_dng_sdk_archive(builder)
        if not archive_or_dir:
            raise RuntimeError(
                "dng-sdk: missing Adobe DNG SDK source archive.\n"
                "Place it under `external/` (e.g. `external/dng_sdk_1_7_1_0.zip`) or set `DNGSDK_ARCHIVE`."
            )
        if not archive_or_dir.exists():
            raise RuntimeError(f"dng-sdk: archive path does not exist: {archive_or_dir}")

        vendor_root = builder.config.global_cfg.build_root / "_vendor" / "dng-sdk"
        vendor_extract = vendor_root / "src"
        stamp_path = vendor_root / ".stamp"
        vendor_root.mkdir(parents=True, exist_ok=True)

        extracted_root: Path
        if archive_or_dir.is_dir():
            extracted_root = archive_or_dir
        else:
            st = archive_or_dir.stat()
            current = _VendorStamp(path=str(archive_or_dir), size=int(st.st_size), mtime=int(st.st_mtime))
            previous = _read_vendor_stamp(stamp_path)
            if previous != current:
                shutil.rmtree(vendor_extract, ignore_errors=True)
                vendor_extract.mkdir(parents=True, exist_ok=True)
                shutil.unpack_archive(str(archive_or_dir), str(vendor_extract))
                _write_vendor_stamp(stamp_path, archive_or_dir)
            extracted_root = vendor_extract

        dng_sdk_dir = _find_dng_sdk_dir(extracted_root)
        if not dng_sdk_dir:
            raise RuntimeError(
                "dng-sdk: could not locate `dng_sdk/source/dng_host.h` in the provided archive.\n"
                "Ensure you downloaded the official Adobe DNG SDK and provided the correct archive path."
            )
        xmp_dir = _find_xmp_dir(extracted_root)
        if not xmp_dir:
            raise RuntimeError(
                "dng-sdk: could not locate XMP SDK sources in the provided archive.\n"
                "Expected a path like `xmp/toolkit/public/include/XMP.hpp` (or `xmp/public/include/XMP.hpp`).\n"
                "The DNG-CMake project expects the XMP SDK sources from the Adobe DNG SDK archive."
            )

        if not dng_expected.exists():
            dst = src_dir / "dng_sdk"
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(dng_sdk_dir, dst)
        dst_xmp = src_dir / "xmp"
        dst_toolkit = dst_xmp / "toolkit"
        need_xmp = not (xmp_expected_header.exists() and xmp_expected_source.exists())
        if need_xmp:
            shutil.rmtree(dst_xmp, ignore_errors=True)
            dst_xmp.mkdir(parents=True, exist_ok=True)
            if xmp_dir.name == "xmp":
                if (xmp_dir / "toolkit").is_dir():
                    shutil.copytree(xmp_dir, dst_xmp, dirs_exist_ok=True)
                else:
                    shutil.copytree(xmp_dir, dst_toolkit, dirs_exist_ok=True)
            else:
                # Fall back to a toolkit-like root.
                shutil.copytree(xmp_dir, dst_toolkit, dirs_exist_ok=True)

    top_cmake = src_dir / "CMakeLists.txt"
    if top_cmake.exists():
        top_text = top_cmake.read_text(encoding="utf-8", errors="replace")
        top_changed = False

        if 'option(BUILD_DNG_VALIDATE "Build the dng_validate tool" ON)' in top_text:
            top_text = top_text.replace(
                'option(BUILD_DNG_VALIDATE "Build the dng_validate tool" ON)',
                'option(BUILD_DNG_VALIDATE "Build the dng_validate tool (independent from library validation)" ON)',
                1,
            )
            top_changed = True

        auto_validate_block = """set(DNG_VALIDATE "AUTO" CACHE STRING "Enable validation checks in the dng_sdk library: AUTO=Debug only, ON=all configs, OFF=disabled")
set_property(CACHE DNG_VALIDATE PROPERTY STRINGS AUTO ON OFF)"""
        if auto_validate_block not in top_text:
            validate_options = [
                'option(DNG_VALIDATE "Enable validation checks (qDNGValidate)" OFF)',
                'option(DNG_VALIDATE "Enable validation checks in the dng_sdk library (qDNGValidate)" OFF)',
            ]
            for validate_option in validate_options:
                if validate_option in top_text:
                    top_text = top_text.replace(validate_option, auto_validate_block, 1)
                    top_changed = True
                    break

        if top_changed:
            top_cmake.write_text(top_text, encoding="utf-8")

    # DNG-CMake currently lists dng_jxl.cpp unconditionally as a source file.
    # That breaks configurations that explicitly disable JXL (DNG_WITH_JXL=OFF).
    dng_sdk_cmake = src_dir / "cmake" / "dng_sdk.cmake"
    if dng_sdk_cmake.exists():
        cmake_text = dng_sdk_cmake.read_text(encoding="utf-8", errors="replace")
        lines = cmake_text.splitlines()
        changed = False

        # Patch 1: guard dng_jxl.cpp behind DNG_WITH_JXL.
        if "OIIO_BUILDER_DNGSDK_JXL_GUARD_BEGIN" not in cmake_text:
            cleaned_lines: list[str] = []
            removed_jxl = False
            for line in lines:
                if "${CMAKE_SOURCE_DIR}/dng_sdk/source/dng_jxl.cpp" in line:
                    removed_jxl = True
                    continue
                cleaned_lines.append(line)

            insert_at: int | None = None
            in_add_library = False
            for idx, line in enumerate(cleaned_lines):
                stripped = line.strip()
                if stripped.startswith("add_library(dng_sdk"):
                    in_add_library = True
                    continue
                if in_add_library and stripped == ")":
                    insert_at = idx + 1
                    break

            if removed_jxl and insert_at is not None:
                guard_block = [
                    "",
                    "# OIIO_BUILDER_DNGSDK_JXL_GUARD_BEGIN",
                    "if(DNG_WITH_JXL)",
                    "    target_sources(dng_sdk PRIVATE ${CMAKE_SOURCE_DIR}/dng_sdk/source/dng_jxl.cpp)",
                    "endif()",
                    "# OIIO_BUILDER_DNGSDK_JXL_GUARD_END",
                    "",
                ]
                cleaned_lines[insert_at:insert_at] = guard_block
                lines = cleaned_lines
                changed = True

        # Patch 2: keep library validation independent from the dng_validate tool.
        #
        # DNG_VALIDATE controls qDNGValidate for the dng_sdk library. BUILD_DNG_VALIDATE
        # controls the executable, which compiles its own dng_globals.cpp object with
        # qDNGValidateTarget=1 (see dng_validate.cmake patch below).
        validate_comment = "DNG_VALIDATE controls validation code in the dng_sdk library only."
        if validate_comment not in "\n".join(lines):
            start: int | None = None
            end: int | None = None
            for i, line in enumerate(lines):
                if "OIIO_BUILDER_DNGSDK_VALIDATE_GLOBALS_BEGIN" in line:
                    start = i
                    for j in range(i + 1, len(lines)):
                        if "OIIO_BUILDER_DNGSDK_VALIDATE_GLOBALS_END" in lines[j]:
                            end = j
                            break
                    break

            if start is None:
                for i, line in enumerate(lines):
                    if "set_source_files_properties" in line:
                        start = i
                        continue
                    if start is not None and i > start and line.strip() == ")":
                        end = i
                        block = "\n".join(lines[start : end + 1])
                        if "dng_globals.cpp" in block and "qDNGValidate=1" in block:
                            break
                        start = None
                        end = None

            if start is not None and end is not None:
                if start > 0 and not lines[start - 1].strip():
                    start -= 1
                if end + 1 < len(lines) and not lines[end + 1].strip():
                    end += 1
                replacement = [
                    "",
                    f"# {validate_comment}",
                    "# BUILD_DNG_VALIDATE builds the validator executable through",
                    "# cmake/dng_validate.cmake without forcing validation into this library.",
                    "",
                ]
                lines[start : end + 1] = replacement
                changed = True

        validate_mode_marker = "Invalid DNG_VALIDATE='${DNG_VALIDATE}'"
        if validate_mode_marker not in "\n".join(lines):
            start = next(
                (
                    i
                    for i, line in enumerate(lines)
                    if line.strip() == "target_compile_definitions(dng_sdk PRIVATE qDNGValidateTarget=0)"
                ),
                None,
            )
            if start is not None:
                end = start
                if start + 1 < len(lines) and lines[start + 1].strip() == "if(DNG_VALIDATE)":
                    for j in range(start + 2, len(lines)):
                        if lines[j].strip() == "endif()":
                            end = j
                            break
                replacement = [
                    "target_compile_definitions(dng_sdk PRIVATE qDNGValidateTarget=0)",
                    'string(TOUPPER "${DNG_VALIDATE}" DNG_VALIDATE_MODE)',
                    'if(NOT DNG_VALIDATE_MODE MATCHES "^(AUTO|ON|OFF|TRUE|FALSE|YES|NO|1|0)$")',
                    '    message(FATAL_ERROR "Invalid DNG_VALIDATE=\'${DNG_VALIDATE}\'. Expected AUTO, ON, or OFF.")',
                    "endif()",
                    'if(DNG_VALIDATE_MODE STREQUAL "AUTO")',
                    "    target_compile_definitions(dng_sdk PRIVATE $<$<CONFIG:Debug>:qDNGValidate=1>)",
                    "elseif(DNG_VALIDATE)",
                    "    target_compile_definitions(dng_sdk PRIVATE qDNGValidate=1)",
                    "endif()",
                ]
                lines[start : end + 1] = replacement
                changed = True

        if changed:
            dng_sdk_cmake.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dng_validate_cmake = src_dir / "cmake" / "dng_validate.cmake"
    if dng_validate_cmake.exists():
        validate_text = dng_validate_cmake.read_text(encoding="utf-8", errors="replace")
        validate_lines = validate_text.splitlines()
        validate_changed = False

        shim_path = src_dir / "cmake" / "dng_validate_globals.cpp"
        if shim_path.exists():
            try:
                shim_path.unlink()
            except OSError:
                pass

        marker = "dng_validate owns its validation globals independently from dng_sdk."
        desired_block = """\
# dng_validate owns its validation globals independently from dng_sdk.
# This keeps BUILD_DNG_VALIDATE separate from the DNG_VALIDATE library option.
target_sources(dng_validate PRIVATE
    ${CMAKE_SOURCE_DIR}/dng_sdk/source/dng_globals.cpp
)
""".rstrip("\n").splitlines()

        if marker in validate_text:
            begin = next((i for i, line in enumerate(validate_lines) if marker in line), None)
            if begin is not None:
                end = None
                for j in range(begin + 1, len(validate_lines)):
                    if validate_lines[j].strip() == ")":
                        end = j
                        break
                if end is not None and validate_lines[begin : end + 1] != desired_block:
                    validate_lines[begin : end + 1] = desired_block
                    validate_changed = True
        else:
            old_begin = next(
                (i for i, line in enumerate(validate_lines) if "OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_BEGIN" in line),
                None,
            )
            if old_begin is not None:
                old_end = None
                for j in range(old_begin + 1, len(validate_lines)):
                    if "OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_END" in validate_lines[j]:
                        old_end = j
                        break
                if old_end is not None:
                    validate_lines[old_begin : old_end + 1] = desired_block
                    validate_changed = True

        if marker not in "\n".join(validate_lines):
            # Insert right after the add_executable(dng_validate ...) block.
            insert_at = None
            depth = 0
            in_add = False
            for i, line in enumerate(validate_lines):
                stripped = line.strip()
                if stripped.startswith("add_executable(dng_validate"):
                    in_add = True
                    depth = stripped.count("(") - stripped.count(")")
                    continue
                if in_add:
                    depth += stripped.count("(") - stripped.count(")")
                    if depth <= 0 and stripped == ")":
                        insert_at = i + 1
                        break
            if insert_at is not None:
                block_lines = desired_block[:]
                block_lines.insert(0, "")
                validate_lines[insert_at:insert_at] = block_lines
                validate_changed = True

        if validate_changed:
            dng_validate_cmake.write_text("\n".join(validate_lines) + "\n", encoding="utf-8")

    xmp_config_in = src_dir / "cmake" / "XMPToolkit-config.cmake.in"
    if xmp_config_in.exists():
        original_text = xmp_config_in.read_text(encoding="utf-8", errors="replace")
        lines = original_text.splitlines()
        changed = False

        expat_block = """\
# OIIO_BUILDER_EXPAT_TARGET_BEGIN
if(NOT TARGET EXPAT::EXPAT)
    if(TARGET expat::expat)
        add_library(EXPAT::EXPAT ALIAS expat::expat)
    elseif(TARGET PkgConfig::EXPAT)
        add_library(EXPAT::EXPAT ALIAS PkgConfig::EXPAT)
    elseif(EXPAT_LIBRARY)
        add_library(EXPAT::EXPAT UNKNOWN IMPORTED)
        set_imported_location_all_configs(EXPAT::EXPAT "${EXPAT_LIBRARY}")
        if(EXPAT_INCLUDE_DIR)
            set_target_properties(EXPAT::EXPAT PROPERTIES
                INTERFACE_INCLUDE_DIRECTORIES "${EXPAT_INCLUDE_DIR}"
            )
        elseif(EXPAT_INCLUDE_DIRS)
            set_target_properties(EXPAT::EXPAT PROPERTIES
                INTERFACE_INCLUDE_DIRECTORIES "${EXPAT_INCLUDE_DIRS}"
            )
        endif()
    elseif(EXPAT_LIBRARIES)
        add_library(EXPAT::EXPAT INTERFACE IMPORTED)
        set_property(TARGET EXPAT::EXPAT PROPERTY INTERFACE_LINK_LIBRARIES ${EXPAT_LIBRARIES})
        if(EXPAT_INCLUDE_DIR)
            set_property(TARGET EXPAT::EXPAT PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${EXPAT_INCLUDE_DIR}")
        elseif(EXPAT_INCLUDE_DIRS)
            set_property(TARGET EXPAT::EXPAT PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${EXPAT_INCLUDE_DIRS}")
        endif()
    endif()
endif()
# OIIO_BUILDER_EXPAT_TARGET_END
"""

        zlib_block = """\
# OIIO_BUILDER_ZLIB_TARGET_BEGIN
if(NOT TARGET ZLIB::ZLIB)
    if(TARGET PkgConfig::ZLIB)
        add_library(ZLIB::ZLIB ALIAS PkgConfig::ZLIB)
    elseif(ZLIB_LIBRARY)
        add_library(ZLIB::ZLIB UNKNOWN IMPORTED)
        set_imported_location_all_configs(ZLIB::ZLIB "${ZLIB_LIBRARY}")
        if(ZLIB_INCLUDE_DIR)
            set_target_properties(ZLIB::ZLIB PROPERTIES
                INTERFACE_INCLUDE_DIRECTORIES "${ZLIB_INCLUDE_DIR}"
            )
        elseif(ZLIB_INCLUDE_DIRS)
            set_target_properties(ZLIB::ZLIB PROPERTIES
                INTERFACE_INCLUDE_DIRECTORIES "${ZLIB_INCLUDE_DIRS}"
            )
        endif()
    elseif(ZLIB_LIBRARIES)
        add_library(ZLIB::ZLIB INTERFACE IMPORTED)
        set_property(TARGET ZLIB::ZLIB PROPERTY INTERFACE_LINK_LIBRARIES ${ZLIB_LIBRARIES})
        if(ZLIB_INCLUDE_DIR)
            set_property(TARGET ZLIB::ZLIB PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${ZLIB_INCLUDE_DIR}")
        elseif(ZLIB_INCLUDE_DIRS)
            set_property(TARGET ZLIB::ZLIB PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${ZLIB_INCLUDE_DIRS}")
        endif()
    endif()
endif()
# OIIO_BUILDER_ZLIB_TARGET_END
"""

        def _upsert_block(marker: str, end_marker: str, desired: list[str], anchor: str) -> None:
            nonlocal lines, changed
            begin = next((i for i, line in enumerate(lines) if marker in line), None)
            if begin is not None:
                end = None
                for j in range(begin + 1, len(lines)):
                    if end_marker in lines[j]:
                        end = j
                        break
                if end is None:
                    return
                if lines[begin : end + 1] != desired:
                    lines[begin : end + 1] = desired
                    changed = True
                return

            anchor_idx = next((i for i, line in enumerate(lines) if line.strip() == anchor), None)
            if anchor_idx is None:
                return
            insert_lines = desired[:]
            if anchor_idx > 0 and lines[anchor_idx - 1].strip() != "":
                insert_lines.insert(0, "")
            if lines[anchor_idx].strip() != "":
                insert_lines.append("")
            lines[anchor_idx:anchor_idx] = insert_lines
            changed = True

        _upsert_block(
            "OIIO_BUILDER_EXPAT_TARGET_BEGIN",
            "OIIO_BUILDER_EXPAT_TARGET_END",
            expat_block.rstrip("\n").splitlines(),
            "# Zlib compression library",
        )
        _upsert_block(
            "OIIO_BUILDER_ZLIB_TARGET_BEGIN",
            "OIIO_BUILDER_ZLIB_TARGET_END",
            zlib_block.rstrip("\n").splitlines(),
            "# Include the targets file - this creates the XMP::* targets",
        )

        if changed:
            xmp_config_in.write_text("\n".join(lines) + "\n", encoding="utf-8")

    dng_config_in = src_dir / "cmake" / "dng_sdk-config.cmake.in"
    if dng_config_in.exists():
        dng_config_text = dng_config_in.read_text(encoding="utf-8", errors="replace")
        dng_config_changed = False

        configured_location_marker = "function(set_imported_location_configured target release_location debug_location)"
        if configured_location_marker not in dng_config_text:
            all_configs_helper = """function(set_imported_location_all_configs target location)
    if(location)
        set_target_properties(${target} PROPERTIES
            IMPORTED_LOCATION "${location}"
            IMPORTED_LOCATION_RELEASE "${location}"
            IMPORTED_LOCATION_MINSIZEREL "${location}"
            IMPORTED_LOCATION_RELWITHDEBINFO "${location}"
            IMPORTED_LOCATION_DEBUG "${location}"
        )
    endif()
endfunction()
"""
            configured_helper = all_configs_helper + """
# Helper function to set imported library locations for single- and multi-config consumers.
function(set_imported_location_configured target release_location debug_location)
    set(_release "${release_location}")
    set(_debug "${debug_location}")
    if(NOT _release AND _debug)
        set(_release "${_debug}")
    endif()
    if(NOT _debug AND _release)
        set(_debug "${_release}")
    endif()
    set(_any "${_release}")
    if(NOT _any)
        set(_any "${_debug}")
    endif()
    if(_any)
        set_target_properties(${target} PROPERTIES
            IMPORTED_LOCATION "${_any}"
            IMPORTED_LOCATION_RELEASE "${_release}"
            IMPORTED_LOCATION_MINSIZEREL "${_release}"
            IMPORTED_LOCATION_RELWITHDEBINFO "${_release}"
            IMPORTED_LOCATION_DEBUG "${_debug}"
        )
    endif()
endfunction()
"""
            if all_configs_helper in dng_config_text:
                dng_config_text = dng_config_text.replace(all_configs_helper, configured_helper, 1)
                dng_config_changed = True

        jxl_marker = "OIIO_BUILDER_JXL_DEBUG_LIBRARY_NAMES_BEGIN"
        if jxl_marker not in dng_config_text:
            old_find_block = """        find_library(JXL_LIBRARY NAMES jxl REQUIRED)
        find_library(JXL_THREADS_LIBRARY NAMES jxl_threads REQUIRED)
        find_library(JXL_CMS_LIBRARY NAMES jxl_cms)
        find_library(HWY_LIBRARY NAMES hwy REQUIRED)
        find_library(BROTLI_COMMON_LIBRARY NAMES brotlicommon REQUIRED)
        find_library(BROTLI_DEC_LIBRARY NAMES brotlidec REQUIRED)
        find_library(BROTLI_ENC_LIBRARY NAMES brotlienc REQUIRED)
"""
            new_find_block = """        # OIIO_BUILDER_JXL_DEBUG_LIBRARY_NAMES_BEGIN
        # Windows Debug installs commonly use a "d" postfix (jxld.lib, hwyd.lib,
        # brotlicommond.lib). Find both config-specific names and expose them on
        # imported targets; non-Windows installs keep using the normal names.
        macro(_dng_find_dependency_library _var _release_name _debug_name)
            set(_required FALSE)
            foreach(_arg ${ARGN})
                if(_arg STREQUAL "REQUIRED")
                    set(_required TRUE)
                endif()
            endforeach()
            find_library(${_var}_RELEASE NAMES ${_release_name})
            find_library(${_var}_DEBUG NAMES ${_debug_name} ${_release_name})
            if(NOT ${_var}_RELEASE AND ${_var}_DEBUG)
                set(${_var}_RELEASE "${${_var}_DEBUG}")
            endif()
            if(NOT ${_var}_DEBUG AND ${_var}_RELEASE)
                set(${_var}_DEBUG "${${_var}_RELEASE}")
            endif()
            set(${_var} "${${_var}_DEBUG}")
            if(NOT ${_var})
                set(${_var} "${${_var}_RELEASE}")
            endif()
            if(_required AND NOT ${_var}_RELEASE AND NOT ${_var}_DEBUG)
                message(FATAL_ERROR "Could not find ${_var} using names: ${_release_name}, ${_debug_name}")
            endif()
        endmacro()

        _dng_find_dependency_library(JXL_LIBRARY jxl jxld REQUIRED)
        _dng_find_dependency_library(JXL_THREADS_LIBRARY jxl_threads jxl_threadsd REQUIRED)
        _dng_find_dependency_library(JXL_CMS_LIBRARY jxl_cms jxl_cmsd)
        _dng_find_dependency_library(HWY_LIBRARY hwy hwyd REQUIRED)
        _dng_find_dependency_library(BROTLI_COMMON_LIBRARY brotlicommon brotlicommond REQUIRED)
        _dng_find_dependency_library(BROTLI_DEC_LIBRARY brotlidec brotlidecd REQUIRED)
        _dng_find_dependency_library(BROTLI_ENC_LIBRARY brotlienc brotliencd REQUIRED)
        # OIIO_BUILDER_JXL_DEBUG_LIBRARY_NAMES_END
"""
            if old_find_block in dng_config_text:
                dng_config_text = dng_config_text.replace(old_find_block, new_find_block, 1)
                dng_config_changed = True

            replacements = {
                'set_imported_location_all_configs(jxl::jxl "${JXL_LIBRARY}")':
                    'set_imported_location_configured(jxl::jxl "${JXL_LIBRARY_RELEASE}" "${JXL_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(jxl::jxl_threads "${JXL_THREADS_LIBRARY}")':
                    'set_imported_location_configured(jxl::jxl_threads "${JXL_THREADS_LIBRARY_RELEASE}" "${JXL_THREADS_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(jxl::jxl_cms "${JXL_CMS_LIBRARY}")':
                    'set_imported_location_configured(jxl::jxl_cms "${JXL_CMS_LIBRARY_RELEASE}" "${JXL_CMS_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(hwy::hwy "${HWY_LIBRARY}")':
                    'set_imported_location_configured(hwy::hwy "${HWY_LIBRARY_RELEASE}" "${HWY_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(brotli::brotlicommon "${BROTLI_COMMON_LIBRARY}")':
                    'set_imported_location_configured(brotli::brotlicommon "${BROTLI_COMMON_LIBRARY_RELEASE}" "${BROTLI_COMMON_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(brotli::brotlidec "${BROTLI_DEC_LIBRARY}")':
                    'set_imported_location_configured(brotli::brotlidec "${BROTLI_DEC_LIBRARY_RELEASE}" "${BROTLI_DEC_LIBRARY_DEBUG}")',
                'set_imported_location_all_configs(brotli::brotlienc "${BROTLI_ENC_LIBRARY}")':
                    'set_imported_location_configured(brotli::brotlienc "${BROTLI_ENC_LIBRARY_RELEASE}" "${BROTLI_ENC_LIBRARY_DEBUG}")',
            }
            for old, new in replacements.items():
                if old in dng_config_text:
                    dng_config_text = dng_config_text.replace(old, new, 1)
                    dng_config_changed = True

        marker = "OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_BEGIN"
        if marker not in dng_config_text:
            dng_lines = dng_config_text.splitlines()
            insert_at = next(
                (
                    idx
                    for idx, line in enumerate(dng_lines)
                    if "if((_dng_lcms2_release OR _dng_lcms2_debug) AND NOT TARGET dng_sdk::lcms2)" in line
                ),
                None,
            )
            if insert_at is not None:
                fallback_block = [
                    "        # OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_BEGIN",
                    "        # Some installs expose only one configuration for lcms2::lcms2.",
                    "        # Mirror the available location so imported targets are valid",
                    "        # across single- and multi-config generators.",
                    "        if(NOT _dng_lcms2_release AND _dng_lcms2_debug)",
                    "            set(_dng_lcms2_release \"${_dng_lcms2_debug}\")",
                    "        endif()",
                    "        if(NOT _dng_lcms2_debug AND _dng_lcms2_release)",
                    "            set(_dng_lcms2_debug \"${_dng_lcms2_release}\")",
                    "        endif()",
                    "        # OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_END",
                    "",
                ]
                dng_lines[insert_at:insert_at] = fallback_block
                dng_config_text = "\n".join(dng_lines) + "\n"
                dng_config_changed = True

        if dng_config_changed:
            dng_config_in.write_text(dng_config_text, encoding="utf-8")

    # DNG-CMake carries a patch file (cmake/xmp_stream_io_cstring.patch) that
    # adds <cstring>, but it can fail to apply as upstream XMP sources evolve.
    # Clang/libstdc++ is strict enough that missing memcpy declarations become
    # hard errors, so ensure the include is present.
    xmp_stream_io = src_dir / "xmp" / "toolkit" / "source" / "XMPStream_IO.cpp"
    if xmp_stream_io.exists():
        text = xmp_stream_io.read_text(encoding="utf-8", errors="replace")
        if "#include <cstring>" not in text and "#include <string.h>" not in text:
            lines = text.splitlines()
            insert_at = None
            for idx, line in enumerate(lines):
                if line.strip().startswith("#define TwoGB"):
                    insert_at = idx
                    break
            if insert_at is None:
                last_include = None
                for idx, line in enumerate(lines):
                    if line.lstrip().startswith("#include"):
                        last_include = idx
                if last_include is not None:
                    insert_at = last_include + 1
            if insert_at is None:
                insert_at = 0
            block = ["", "#include <cstring>", ""]
            if insert_at > 0 and lines[insert_at - 1].strip() == "":
                block = ["#include <cstring>", ""]
            lines[insert_at:insert_at] = block
            xmp_stream_io.write_text("\n".join(lines) + "\n", encoding="utf-8")
