from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled

STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def patch_source(_builder, src_dir: Path) -> None:
    _patch_system_zlib_spng_targets(src_dir)
    _patch_system_zlib_package_config(src_dir)


def _patch_system_zlib_spng_targets(src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        raise RuntimeError(
            f"libjpeg-turbo system-ZLIB spng patch target is missing: {cmake_lists}"
        )

    original_text = cmake_lists.read_text(encoding="utf-8", errors="replace")
    text = original_text
    marker_begin = "# OIIO_BUILDER_SYSTEM_ZLIB_SPNG_BEGIN"
    marker_end = "# OIIO_BUILDER_SYSTEM_ZLIB_SPNG_END"
    block = """\
# OIIO_BUILDER_SYSTEM_ZLIB_SPNG_BEGIN
# Bundled libspng object targets include zlib.h directly.  Propagate the
# system ZLIB target's include directories as well as its link requirement.
if(WITH_SYSTEM_ZLIB)
  if(TARGET spng-static)
    target_link_libraries(spng-static PRIVATE ZLIB::ZLIB)
  endif()
  if(TARGET spng)
    target_link_libraries(spng PRIVATE ZLIB::ZLIB)
  endif()
endif()
# OIIO_BUILDER_SYSTEM_ZLIB_SPNG_END

"""

    if marker_begin in text and marker_end in text:
        start = text.index(marker_begin)
        stop = text.index(marker_end, start) + len(marker_end)
        if text[stop : stop + 2] == "\r\n":
            stop += 2
        elif text[stop : stop + 1] == "\n":
            stop += 1
        if text[stop : stop + 1] == "\n":
            stop += 1
        text = text[:start] + block + text[stop:]
    elif not (
        "target_link_libraries(spng-static PRIVATE ZLIB::ZLIB)" in text
        and "target_link_libraries(spng PRIVATE ZLIB::ZLIB)" in text
    ):
        anchor = "if(ENABLE_STATIC)\n"
        shared_block = "if(ENABLE_SHARED)\n  add_subdirectory(sharedlib)\nendif()\n\n"
        insertion_anchor = shared_block + anchor
        if insertion_anchor not in text:
            raise RuntimeError(
                "libjpeg-turbo system-ZLIB spng patch no longer matches "
                f"upstream source: {cmake_lists}"
            )
        text = text.replace(insertion_anchor, shared_block + block + anchor, 1)

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")


def _patch_system_zlib_package_config(src_dir: Path) -> None:
    config_template = src_dir / "release" / "Config.cmake.in"
    if not config_template.exists():
        raise RuntimeError(
            f"libjpeg-turbo ZLIB package dependency patch target is missing: {config_template}"
        )

    original_text = config_template.read_text(encoding="utf-8", errors="replace")
    text = original_text
    marker_begin = "# OIIO_BUILDER_SYSTEM_ZLIB_CONFIG_BEGIN"
    marker_end = "# OIIO_BUILDER_SYSTEM_ZLIB_CONFIG_END"
    block = """\
# OIIO_BUILDER_SYSTEM_ZLIB_CONFIG_BEGIN
# System-zlib TurboJPEG targets export ZLIB::ZLIB in their link interface.
if("@WITH_SYSTEM_ZLIB@" OR "@WITH_SYSTEM_SPNG@")
  include(CMakeFindDependencyMacro)
  find_dependency(ZLIB)
endif()
# OIIO_BUILDER_SYSTEM_ZLIB_CONFIG_END

"""

    if marker_begin in text and marker_end in text:
        start = text.index(marker_begin)
        stop = text.index(marker_end, start) + len(marker_end)
        if text[stop : stop + 2] == "\r\n":
            stop += 2
        elif text[stop : stop + 1] == "\n":
            stop += 1
        if text[stop : stop + 1] == "\n":
            stop += 1
        text = text[:start] + block + text[stop:]
    elif "find_dependency(ZLIB" not in text:
        anchor = 'include("${CMAKE_CURRENT_LIST_DIR}/@CMAKE_PROJECT_NAME@Targets.cmake")'
        if anchor not in text:
            raise RuntimeError(
                "libjpeg-turbo ZLIB package dependency patch no longer matches "
                f"upstream source: {config_template}"
            )
        text = text.replace(anchor, block + anchor, 1)

    if text != original_text:
        config_template.write_text(text, encoding="utf-8")
