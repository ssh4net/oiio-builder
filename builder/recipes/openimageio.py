from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_oiio)


def patch_source(builder, src_dir: Path) -> None:
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
