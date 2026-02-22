from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "3"


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    if not getattr(cfg, "build_dng_sdk", False):
        return []
    return [
        "-DENABLE_DNGSDK=ON",
        f"-DDNGSDK_ROOT={ctx.install_prefix}",
    ]


def patch_source(_builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_text(encoding="utf-8", errors="replace")
    lines = original_text.splitlines()
    changed = False

    # LibRaw-cmake's CMakeLists declares LANGUAGES CXX only, but it optionally
    # builds sample tools from `.c` sources (dcraw_half.c / half_mt.c). Without
    # enabling C, CMake will ignore those sources and produce executables with
    # no objects, failing to link with "undefined symbol: main".
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("project(") or "libraw" not in stripped:
            continue
        if "LANGUAGES CXX" in line and "LANGUAGES C CXX" not in line:
            lines[i] = line.replace("LANGUAGES CXX", "LANGUAGES C CXX")
            changed = True
        break

    text = "\n".join(lines) + ("\n" if original_text.endswith("\n") else "")

    option_block = """\
# OIIO_BUILDER_DNGSDK_BEGIN
option(ENABLE_DNGSDK "Build library with Adobe DNG SDK support (USE_DNGSDK)" OFF)
set(DNGSDK_ROOT "" CACHE PATH "Prefix containing the DNG SDK install (include/ and lib/)")
# OIIO_BUILDER_DNGSDK_END
"""

    apply_block = """\
# OIIO_BUILDER_DNGSDK_BEGIN
if(ENABLE_DNGSDK)
    message(STATUS "Check for Adobe DNG SDK availability...")

    if(DNGSDK_ROOT)
        list(PREPEND CMAKE_PREFIX_PATH "${DNGSDK_ROOT}")
    endif()

    # Prefer the CMake package produced by DNG-CMake (required for static builds
    # to propagate platform macros and transitive libs like libjxl/XMP).
    find_package(dng_sdk CONFIG REQUIRED)

    foreach(_oiio_builder_tgt raw raw_r)
        target_compile_definitions(${_oiio_builder_tgt} PUBLIC USE_DNGSDK)
        target_link_libraries(${_oiio_builder_tgt} PUBLIC dng_sdk::dng_sdk)
    endforeach()
endif()
# OIIO_BUILDER_DNGSDK_END
"""

    marker = "OIIO_BUILDER_DNGSDK_BEGIN"
    if marker in text:
        lines = text.splitlines()
        begins = [i for i, line in enumerate(lines) if marker in line]
        if len(begins) < 2:
            return

        def _find_end(start: int) -> int | None:
            for j in range(start + 1, len(lines)):
                if "OIIO_BUILDER_DNGSDK_END" in lines[j]:
                    return j
            return None

        blocks = [option_block.rstrip("\n").splitlines(), apply_block.rstrip("\n").splitlines()]
        ranges: list[tuple[int, int, list[str]]] = []
        for idx, begin in enumerate(begins[:2]):
            end = _find_end(begin)
            if end is None:
                return
            ranges.append((begin, end, blocks[idx]))

        blocks_changed = False
        for begin, end, replacement in reversed(ranges):
            if lines[begin : end + 1] != replacement:
                lines[begin : end + 1] = replacement
                blocks_changed = True
        if blocks_changed:
            cmake_lists.write_text("\n".join(lines) + "\n", encoding="utf-8")
        elif changed:
            cmake_lists.write_text(text, encoding="utf-8")
        return

    lines = text.splitlines()
    inserted_option = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("option(LIBRAW_INSTALL"):
            lines.insert(i + 1, option_block.rstrip("\n"))
            inserted_option = True
            break

    if not inserted_option:
        return

    inserted_apply = False
    for i, line in enumerate(lines):
        if line.startswith("# -- Files to install"):
            lines.insert(i, apply_block.rstrip("\n"))
            inserted_apply = True
            break

    if not inserted_apply:
        return

    cmake_lists.write_text("\n".join(lines) + "\n", encoding="utf-8")
