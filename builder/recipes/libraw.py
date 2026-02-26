from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "4"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_libraw)


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    if not getattr(cfg, "build_dng_sdk", False):
        return []
    args: list[str] = [
        "-DENABLE_DNGSDK=ON",
        f"-DDNGSDK_ROOT={ctx.install_prefix}",
    ]

    if builder.platform.os != "windows":
        return args

    lib_dir = (ctx.install_prefix / "lib").resolve()
    debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
    is_debug = ctx.build_type == "Debug"

    def _pick(stems: list[str]) -> Path | None:
        candidates: list[Path] = []
        if is_debug:
            for stem in stems:
                candidates.extend(
                    [
                        lib_dir / f"{stem}{debug_postfix}.lib",
                        lib_dir / f"lib{stem}{debug_postfix}.lib",
                        lib_dir / f"{stem}.lib",
                        lib_dir / f"lib{stem}.lib",
                    ]
                )
        else:
            for stem in stems:
                candidates.extend(
                    [
                        lib_dir / f"{stem}.lib",
                        lib_dir / f"lib{stem}.lib",
                        lib_dir / f"{stem}{debug_postfix}.lib",
                        lib_dir / f"lib{stem}{debug_postfix}.lib",
                    ]
                )
        for candidate in candidates:
            if candidate.exists():
                return candidate

        matches: list[Path] = []
        for stem in stems:
            matches.extend(sorted(lib_dir.glob(f"{stem}*.lib")))
            matches.extend(sorted(lib_dir.glob(f"lib{stem}*.lib")))
        return matches[0] if matches else None

    mapping = {
        "JXL_LIBRARY": ["jxl"],
        "JXL_THREADS_LIBRARY": ["jxl_threads"],
        "JXL_CMS_LIBRARY": ["jxl_cms"],
        "HWY_LIBRARY": ["hwy"],
        "BROTLI_COMMON_LIBRARY": ["brotlicommon"],
        "BROTLI_DEC_LIBRARY": ["brotlidec"],
        "BROTLI_ENC_LIBRARY": ["brotlienc"],
    }
    for var, stems in mapping.items():
        path = _pick(stems)
        if path is not None:
            args.append(f"-D{var}={path}")

    return args


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
