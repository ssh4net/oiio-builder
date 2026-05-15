from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "7"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_libraw)


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    args: list[str] = [
        f"-DLIBRAW_PATH={cfg.src_root / 'LibRaw'}",
        f"-DENABLE_EXAMPLES={cfg.libraw_enable_examples}",
        "-DENABLE_RAWSPEED=OFF",
        f"-DENABLE_OPENMP={cfg.libraw_enable_openmp}",
        "-DENABLE_LCMS=ON",
        "-DENABLE_JASPER=ON",
        f"-DENABLE_DCRAW_DEBUG={'ON' if ctx.build_type == 'Debug' else 'OFF'}",
        "-DENABLE_X3FTOOLS=ON",
        "-DENABLE_6BY9RPI=ON",
    ]

    if getattr(cfg, "build_dng_sdk", False):
        args += [
            "-DENABLE_DNGSDK=ON",
            f"-DDNGSDK_ROOT={ctx.install_prefix}",
        ]

        if builder.platform.os == "windows":
            args.extend(_windows_dng_dependency_args(builder, ctx))

    if builder.platform.os == "windows":
        args.extend(_windows_lcms_args(builder, ctx))
        if getattr(cfg, "build_dng_sdk", False):
            args.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={_windows_dng_sdk_include(builder, ctx)}")

    return args


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._ensure_dng_sdk_lcms2_compat(ctx.install_prefix, ctx.build_type)


def _windows_dng_sdk_include(_builder, ctx) -> str:
    include_path = ctx.build_dir / "oiio_builder_libraw_dngsdk.cmake"
    include_path.write_text(
        "\n".join(
            [
                "if(WIN32)",
                "  add_compile_definitions(NOMINMAX)",
                "endif()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return include_path.as_posix()


def _windows_dng_dependency_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
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

    args: list[str] = []
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


def _windows_lcms_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
    lib_dir = (ctx.install_prefix / "lib").resolve()
    include_dir = (ctx.install_prefix / "include").resolve()
    if ctx.build_type == "Debug":
        lcms_names = [
            f"lcms2_static{debug_postfix}.lib",
            f"lcms2{debug_postfix}.lib",
            f"liblcms2{debug_postfix}.lib",
            f"lcms-2{debug_postfix}.lib",
            f"liblcms-2{debug_postfix}.lib",
            "lcms2_static.lib",
            "lcms2.lib",
            "liblcms2.lib",
            "lcms-2.lib",
            "liblcms-2.lib",
        ]
    else:
        lcms_names = [
            "lcms2_static.lib",
            "lcms2.lib",
            "liblcms2.lib",
            "lcms-2.lib",
            "liblcms-2.lib",
            f"lcms2_static{debug_postfix}.lib",
            f"lcms2{debug_postfix}.lib",
            f"liblcms2{debug_postfix}.lib",
            f"lcms-2{debug_postfix}.lib",
            f"liblcms-2{debug_postfix}.lib",
        ]
    lcms_lib = next((lib_dir / name for name in lcms_names if (lib_dir / name).exists()), None)
    if lcms_lib is None:
        if ctx.build_type == "Debug":
            patterns = [
                f"lcms2*{debug_postfix}.lib",
                f"liblcms2*{debug_postfix}.lib",
                "lcms2*.lib",
                "liblcms2*.lib",
            ]
        else:
            patterns = [
                "lcms2*.lib",
                "liblcms2*.lib",
                f"lcms2*{debug_postfix}.lib",
                f"liblcms2*{debug_postfix}.lib",
            ]
        for pattern in patterns:
            matches = sorted(lib_dir.glob(pattern))
            if matches:
                lcms_lib = matches[0]
                break
    if lcms_lib is None or not (include_dir / "lcms2.h").exists():
        return []

    # LibRaw ships its own FindLCMS2.cmake which doesn't look for
    # `lcms2_static`, so force the static library explicitly.
    return [
        f"-DLCMS2_INCLUDE_DIR={include_dir}",
        f"-DLCMS2_LIBRARIES={lcms_lib}",
        f"-DLCMS2_LIBRARY={lcms_lib}",
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

    include_block = """\
# OIIO_BUILDER_INCLUDE_ORDER_BEGIN
# Keep LibRaw source headers ahead of any previously installed prefix headers.
include_directories(BEFORE ${CMAKE_CURRENT_BINARY_DIR}/
                           ${LIBRAW_PATH}/
                  )
# OIIO_BUILDER_INCLUDE_ORDER_END
"""

    include_marker = "OIIO_BUILDER_INCLUDE_ORDER_BEGIN"
    if include_marker not in text:
        old_include_block = """\
include_directories(${CMAKE_CURRENT_BINARY_DIR}/
                    ${LIBRAW_PATH}/
                   )
"""
        if old_include_block in text:
            text = text.replace(old_include_block, include_block, 1)
    else:
        lines = text.splitlines()
        begin = next((i for i, line in enumerate(lines) if include_marker in line), None)
        if begin is not None:
            end = next((i for i in range(begin + 1, len(lines)) if "OIIO_BUILDER_INCLUDE_ORDER_END" in lines[i]), None)
            replacement = include_block.rstrip("\n").splitlines()
            if end is not None and lines[begin : end + 1] != replacement:
                lines[begin : end + 1] = replacement
                text = "\n".join(lines) + "\n"

    target_include_replacements = {
        "target_include_directories(raw\n        PUBLIC": "target_include_directories(raw BEFORE\n        PUBLIC",
        "target_include_directories(raw_r\n        PUBLIC": "target_include_directories(raw_r BEFORE\n        PUBLIC",
    }
    for needle, replacement in target_include_replacements.items():
        if needle in text:
            text = text.replace(needle, replacement, 1)
    patched_text_changed = text != original_text

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
        elif patched_text_changed:
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
