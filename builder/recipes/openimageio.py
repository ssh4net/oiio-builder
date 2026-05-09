from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "5"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_oiio)


def _patch_compiled_fmt_option(src_dir: Path) -> None:
    def replace_once(path: Path, old: str, new: str, description: str) -> None:
        if not path.exists():
            raise RuntimeError(f"OpenImageIO {description} patch target is missing: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        if new in text:
            return
        if old not in text:
            raise RuntimeError(f"OpenImageIO {description} patch no longer matches upstream source: {path}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    externalpackages = src_dir / "src" / "cmake" / "externalpackages.cmake"
    old = """\
# fmtlib
set_option (OIIO_INTERNALIZE_FMT "Copy fmt headers into <install>/include/OpenImageIO/detail/fmt" ON)
checked_find_package (fmt REQUIRED
                      VERSION_MIN 9.0
                      BUILD_LOCAL missing
                     )
get_target_property(FMT_INCLUDE_DIR fmt::fmt-header-only INTERFACE_INCLUDE_DIRECTORIES)
"""
    new = """\
# fmtlib
set_option (OIIO_INTERNALIZE_FMT "Copy fmt headers into <install>/include/OpenImageIO/detail/fmt" ON)
set_option (OIIO_USE_COMPILED_FMT "Link against compiled fmt::fmt instead of header-only fmt" OFF)
if (OIIO_USE_COMPILED_FMT)
    set (OIIO_USE_COMPILED_FMT_VALUE 1)
else ()
    set (OIIO_USE_COMPILED_FMT_VALUE 0)
endif ()
checked_find_package (fmt REQUIRED
                      VERSION_MIN 9.0
                      BUILD_LOCAL missing
                     )
if (OIIO_USE_COMPILED_FMT)
    get_target_property(FMT_INCLUDE_DIR fmt::fmt INTERFACE_INCLUDE_DIRECTORIES)
else ()
    get_target_property(FMT_INCLUDE_DIR fmt::fmt-header-only INTERFACE_INCLUDE_DIRECTORIES)
endif ()
"""
    replace_once(externalpackages, old, new, "compiled fmt CMake option")

    oiioversion = src_dir / "src" / "include" / "OpenImageIO" / "oiioversion.h.in"
    if not oiioversion.exists():
        raise RuntimeError(f"OpenImageIO version header patch target is missing: {oiioversion}")
    text = oiioversion.read_text(encoding="utf-8", errors="replace")
    marker = "#define OIIO_USE_COMPILED_FMT @OIIO_USE_COMPILED_FMT_VALUE@"
    if marker not in text:
        anchor = "#define OIIO_VERSION_RELEASE_TYPE @PROJECT_VERSION_RELEASE_TYPE@\n"
        replacement = anchor + marker + "\n"
        if anchor not in text:
            raise RuntimeError(f"OpenImageIO version header patch no longer matches upstream source: {oiioversion}")
        text = text.replace(anchor, replacement, 1)
        oiioversion.write_text(text, encoding="utf-8")

    fmt_header = src_dir / "src" / "include" / "OpenImageIO" / "detail" / "fmt.h"
    old = """\
// We want the header-only implementation of fmt
#ifndef FMT_HEADER_ONLY
#    define FMT_HEADER_ONLY
#endif

// Disable fmt exceptions
#ifndef FMT_EXCEPTIONS
#    define FMT_EXCEPTIONS 0
#endif
"""
    new = """\
// By default OIIO uses the header-only implementation of fmt. Builds that opt
// into compiled external fmt must use the same mode in OIIO and consumers.
#ifndef OIIO_USE_COMPILED_FMT
#    define OIIO_USE_COMPILED_FMT 0
#endif
#if !OIIO_USE_COMPILED_FMT
#    ifndef FMT_HEADER_ONLY
#        define FMT_HEADER_ONLY
#    endif

// Disable fmt exceptions for the header-only implementation.
#    ifndef FMT_EXCEPTIONS
#        define FMT_EXCEPTIONS 0
#    endif
#endif
"""
    replace_once(fmt_header, old, new, "fmt header mode")

    libutil = src_dir / "src" / "libutil" / "CMakeLists.txt"
    old = """\
    if (OIIO_INTERNALIZE_FMT OR fmt_LOCAL_BUILD)
        add_dependencies(${targetname} fmt_internal_target)
    else ()
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt-header-only)
    endif ()
"""
    new = """\
    if (OIIO_USE_COMPILED_FMT)
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt)
    elseif (OIIO_INTERNALIZE_FMT OR fmt_LOCAL_BUILD)
        add_dependencies(${targetname} fmt_internal_target)
    else ()
        target_link_libraries (${targetname}
                               PUBLIC fmt::fmt-header-only)
    endif ()
"""
    replace_once(libutil, old, new, "libutil fmt linkage")


def _patch_msvc_python_module_link(src_dir: Path) -> None:
    pythonutils = src_dir / "src" / "cmake" / "pythonutils.cmake"
    if not pythonutils.exists():
        raise RuntimeError(f"OpenImageIO python module patch target is missing: {pythonutils}")

    text = pythonutils.read_text(encoding="utf-8", errors="replace")
    new = """\
    set_target_properties(${target_name} PROPERTIES
                          DEBUG_POSTFIX "")
    if (MSVC)
        set_target_properties(${target_name} PROPERTIES
                              PDB_NAME ${target_name}
                              COMPILE_PDB_NAME ${target_name})
        target_link_options (${target_name} PRIVATE /INCREMENTAL:NO)
    endif ()
"""
    if new in text:
        return

    old = """\
    set_target_properties(PyOpenImageIO PROPERTIES
                          DEBUG_POSTFIX "")
"""
    if old not in text:
        raise RuntimeError(f"OpenImageIO python module patch no longer matches upstream source: {pythonutils}")

    pythonutils.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_source(builder, src_dir: Path) -> None:
    if not builder.dry_run:
        _patch_compiled_fmt_option(src_dir)
        _patch_msvc_python_module_link(src_dir)

    cfg = builder.config.global_cfg
    if not getattr(cfg, "build_dng_sdk", False):
        return
    if builder.dry_run:
        return

    find_libraw = src_dir / "src" / "cmake" / "modules" / "FindLibRaw.cmake"
    if not find_libraw.exists():
        return

    text = find_libraw.read_text(encoding="utf-8", errors="replace")
    block = """\
    # OIIO_BUILDER_DNGSDK_BEGIN
    # If LibRaw was compiled with -DUSE_DNGSDK, static consumers must also link
    # the DNG SDK + XMP libraries (and transitive deps).
    #
    # Prefer the CMake package produced by DNG-CMake. It propagates platform
    # compile definitions (qLinux/qWinOS/...) and static transitive deps
    # (XMPCoreStatic/XMPFilesStatic, libjxl/brotli/hwy, Threads, JPEG, ...).
    find_package (dng_sdk CONFIG QUIET)
    if (TARGET dng_sdk::dng_sdk)
        set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} dng_sdk::dng_sdk)
        set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} dng_sdk::dng_sdk)
    else ()
        # Fallback to direct library discovery for older/non-packaged SDK builds.
        find_library (DNGSDK_LIBRARY NAMES dng_sdk dng)
        find_library (XMPCORE_LIBRARY NAMES XMPCoreStatic XMPCore)
        find_library (XMPFILES_LIBRARY NAMES XMPFilesStatic XMPFiles)
        if (DNGSDK_LIBRARY AND XMPCORE_LIBRARY)
            set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${DNGSDK_LIBRARY} ${XMPCORE_LIBRARY})
            set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${DNGSDK_LIBRARY} ${XMPCORE_LIBRARY})
            if (XMPFILES_LIBRARY)
                set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${XMPFILES_LIBRARY})
                set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${XMPFILES_LIBRARY})
            endif ()

            find_package (EXPAT QUIET)
            if (EXPAT_FOUND)
                if (TARGET EXPAT::EXPAT)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} EXPAT::EXPAT)
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} EXPAT::EXPAT)
                elseif (TARGET expat::expat)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} expat::expat)
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} expat::expat)
                elseif (EXPAT_LIBRARIES)
                    set (LibRaw_r_LIBRARIES ${LibRaw_r_LIBRARIES} ${EXPAT_LIBRARIES})
                    set (LibRaw_LIBRARIES ${LibRaw_LIBRARIES} ${EXPAT_LIBRARIES})
                endif ()
            endif ()
        endif ()
    endif ()
    # OIIO_BUILDER_DNGSDK_END
"""

    lines = text.splitlines()
    marker = "OIIO_BUILDER_DNGSDK_BEGIN"
    if marker in text:
        begin: int | None = None
        end: int | None = None
        for i, line in enumerate(lines):
            if marker in line:
                begin = i
                break
        if begin is None:
            return
        for j in range(begin + 1, len(lines)):
            if "OIIO_BUILDER_DNGSDK_END" in lines[j]:
                end = j
                break
        if end is None:
            return
        replacement = block.rstrip("\n").splitlines()
        if lines[begin : end + 1] != replacement:
            lines[begin : end + 1] = replacement
            find_libraw.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    inserted = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("if (MSVC)"):
            lines.insert(i, block.rstrip("\n"))
            inserted = True
            break
    if not inserted:
        return

    find_libraw.write_text("\n".join(lines) + "\n", encoding="utf-8")


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_png16_include_alias(install_prefix)
