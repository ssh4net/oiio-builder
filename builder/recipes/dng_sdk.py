from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil


STAMP_REVISION = "6"


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

        # Patch 2: do not force qDNGValidate into the dng_sdk library.
        #
        # dng_validate needs gVerbose/gDumpLineLimit, which are only present when
        # qDNGValidate||qDNGDebug. Historically, DNG-CMake forced qDNGValidate for
        # dng_globals.cpp so the tool links in Release, but that enables validation
        # code paths in the library build. Keep Release libs "clean" for consumers
        # like LibRaw/OpenImageIO and provide the globals from the dng_validate
        # target instead (see dng_validate.cmake patch below).
        validate_marker = "OIIO_BUILDER_DNGSDK_VALIDATE_GLOBALS_BEGIN"
        if validate_marker not in "\n".join(lines):
            start: int | None = None
            end: int | None = None
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
                replacement = [
                    "",
                    "# OIIO_BUILDER_DNGSDK_VALIDATE_GLOBALS_BEGIN",
                    "# Do not force qDNGValidate for dng_sdk library builds. Release consumers",
                    "# (LibRaw/OpenImageIO) should not compile with qDNGValidate enabled.",
                    "# dng_validate is linked with a small shim that defines gVerbose/gDumpLineLimit",
                    "# when needed (see cmake/dng_validate_globals.cpp).",
                    "# OIIO_BUILDER_DNGSDK_VALIDATE_GLOBALS_END",
                    "",
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

        shim_name = "dng_validate_globals.cpp"
        shim_path = src_dir / "cmake" / shim_name
        shim_contents = """\
#include "dng_globals.h"

#if qDNGValidateTarget
bool gVerbose = false;
uint32 gDumpLineLimit = 100;
#endif
"""
        if not shim_path.exists() or shim_path.read_text(encoding="utf-8", errors="replace") != shim_contents:
            shim_path.write_text(shim_contents, encoding="utf-8")

        marker = "OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_BEGIN"
        desired_block = """\
# OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_BEGIN
# dng_sdk Release builds keep qDNGValidate disabled; provide validate-only globals
# for the dng_validate executable so it links without forcing qDNGValidate into the library.
target_sources(dng_validate PRIVATE
    $<$<NOT:$<CONFIG:Debug>>:${CMAKE_SOURCE_DIR}/cmake/dng_validate_globals.cpp>
)
# OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_END
""".rstrip("\n").splitlines()

        if marker in validate_text:
            begin = next((i for i, line in enumerate(validate_lines) if marker in line), None)
            if begin is not None:
                end = None
                for j in range(begin + 1, len(validate_lines)):
                    if "OIIO_BUILDER_DNGSDK_VALIDATE_SHIM_END" in validate_lines[j]:
                        end = j
                        break
                if end is not None and validate_lines[begin : end + 1] != desired_block:
                    validate_lines[begin : end + 1] = desired_block
                    validate_changed = True
        else:
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
