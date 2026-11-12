from __future__ import annotations

import re
from pathlib import Path

STAMP_REVISION = "11"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return bool(cfg.build_libjxl)


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    enable_openexr = "ON" if cfg.build_exr_stack else "OFF"
    args = [
        "-DBUILD_TESTING=OFF",
        # JPEGXL_STATIC adds -static to executables on Linux, which ASAN
        # does not support. BUILD_SHARED_LIBS still controls the archives.
        f"-DJPEGXL_STATIC={'OFF' if builder.platform.os == 'linux' and ctx.build_type == 'ASAN' else 'ON'}",
        f"-DJPEGXL_ENABLE_TOOLS={cfg.libjxl_enable_tools}",
        f"-DJPEGXL_ENABLE_OPENEXR={enable_openexr}",
        "-DJPEGXL_ENABLE_BENCHMARK=OFF",
        "-DJPEGXL_ENABLE_DEVTOOLS=OFF",
        "-DJPEGXL_ENABLE_EXAMPLES=OFF",
        "-DJPEGXL_ENABLE_DOXYGEN=OFF",
        "-DJPEGXL_ENABLE_MANPAGES=OFF",
        "-DJPEGXL_ENABLE_VIEWERS=OFF",
        "-DJPEGXL_ENABLE_JNI=OFF",
        "-DJPEGXL_ENABLE_PLUGINS=OFF",
        "-DJPEGXL_ENABLE_SKCMS=OFF",
        "-DJPEGXL_ENABLE_SJPEG=OFF",
        "-DJPEGXL_FORCE_SYSTEM_BROTLI=ON",
        "-DJPEGXL_FORCE_SYSTEM_LCMS2=ON",
        "-DJPEGXL_FORCE_SYSTEM_HWY=ON",
        "-DJPEGXL_FORCE_SYSTEM_GTEST=ON",
        "-DJPEGXL_BUNDLE_LIBPNG=OFF",
    ]
    if builder.platform.os == "linux" and ctx.build_type == "ASAN":
        args += [
            "-DBUILD_SHARED_LIBS=OFF",
            "-DJPEGXL_ENABLE_FUZZERS=OFF",
            "-DJPEGXL_ENABLE_TCMALLOC=OFF",
            # SIMD-heavy ASAN translation units are expensive. One shared
            # Ninja pool bounds compile and link work even with --parallel 32.
            "-DCMAKE_JOB_POOLS=libjxl_asan=2",
            "-DCMAKE_JOB_POOL_COMPILE=libjxl_asan",
            "-DCMAKE_JOB_POOL_LINK=libjxl_asan",
        ]
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        lib_dir = (ctx.install_prefix / "lib").resolve()
        include_dir = (ctx.install_prefix / "include").resolve()

        def _pick_lib(base: str):
            if ctx.build_type == "Debug":
                preferred = [f"{base}{debug_postfix}.lib", f"{base}.lib"]
            else:
                preferred = [f"{base}.lib", f"{base}{debug_postfix}.lib"]
            for name in preferred:
                candidate = lib_dir / name
                if candidate.exists():
                    return candidate
            for candidate in sorted(lib_dir.glob(f"{base}*.lib")):
                return candidate
            return None

        brotli_common = _pick_lib("brotlicommon")
        brotli_enc = _pick_lib("brotlienc")
        brotli_dec = _pick_lib("brotlidec")
        if brotli_common and brotli_enc and brotli_dec and (include_dir / "brotli").is_dir():
            args += [
                f"-DBROTLI_INCLUDE_DIR={include_dir}",
                f"-DBROTLICOMMON_LIBRARY={brotli_common}",
                f"-DBROTLIENC_LIBRARY={brotli_enc}",
                f"-DBROTLIDEC_LIBRARY={brotli_dec}",
            ]
        hwy_lib = _pick_lib("hwy")
        if hwy_lib and (include_dir / "hwy").is_dir():
            args += [
                f"-DHWY_INCLUDE_DIR={include_dir}",
                f"-DHWY_LIBRARY={hwy_lib}",
            ]
        lcms_lib = None
        for lcms_base in ("lcms2_static", "lcms2", "liblcms2", "lcms-2", "liblcms-2"):
            lcms_lib = _pick_lib(lcms_base)
            if lcms_lib:
                break
        if lcms_lib and (include_dir / "lcms2.h").exists():
            args += [
                f"-DLCMS2_INCLUDE_DIR={include_dir}",
                f"-DLCMS2_LIBRARY={lcms_lib}",
            ]
    return args


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._ensure_openjph_windows_alias(ctx.install_prefix, ctx.build_type)
    builder._make_openexr_pc_override(ctx.install_prefix, ctx.build_type)
    if ctx.build_type == "Debug":
        builder._ensure_openjph_alias(ctx.install_prefix)


def _uses_msvc(builder) -> bool:
    return (
        builder.platform.os == "windows"
        and builder._windows_generator() in {"msvc", "ninja-msvc"}
    )


def _patch_asan_openexr_rtti(src_dir: Path) -> None:
    path = src_dir / "lib" / "jxl_extras.cmake"
    if not path.exists():
        return
    original = path.read_bytes()
    newline = "\r\n" if b"\r\n" in original else "\n"
    text = original.decode("utf-8").replace("\r\n", "\n")
    block = '''      # OIIO_BUILDER_ASAN_OPENEXR_RTTI
      # Match OpenEXR exception typeinfo; Clang ASAN gives incompatible COMDATs
      # when this adapter inherits libjxl's global -fno-rtti.
      if(CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_BUILD_TYPE STREQUAL "ASAN"
         AND CMAKE_CXX_COMPILER_ID MATCHES "Clang")
        string(APPEND _exr_flags " -frtti")
      endif()
'''
    if block in text:
        return
    anchor = "      set_source_files_properties(extras/dec/exr.cc extras/enc/exr.cc\n"
    if "# OIIO_BUILDER_ASAN_OPENEXR_RTTI" in text or text.count(anchor) != 1:
        raise RuntimeError(f"libjxl ASAN OpenEXR RTTI patch no longer matches {path}")
    text = text.replace(anchor, block + anchor, 1)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))


def patch_source(builder, src_dir) -> None:
    if builder.dry_run:
        return
    _patch_asan_openexr_rtti(src_dir)
    # MSVC 14.44 rejects the standards-valid local constexpr reference in this
    # captureless lambda (C3493). Clang-cl and other compilers accept it, so
    # leave their libjxl sources unchanged.
    if _uses_msvc(builder):
        exr_decoder = src_dir / "lib" / "extras" / "dec" / "exr.cc"
        if exr_decoder.exists():
            original_text = exr_decoder.read_text(encoding="utf-8", errors="replace")
            text = original_text
            marker = "// OIIO_BUILDER_MSVC_EXR_COORD_CAPTURE"
            if marker not in text:
                needle = """\
  auto OutOfRange = [](int v) {
    return v < -kEXRCoordBound || v > kEXRCoordBound;
  };
"""
                replacement = """\
  // OIIO_BUILDER_MSVC_EXR_COORD_CAPTURE
  auto OutOfRange = [kEXRCoordBound](int v) {
    return v < -kEXRCoordBound || v > kEXRCoordBound;
  };
"""
                text = text.replace(needle, replacement, 1)
            if text != original_text:
                exr_decoder.write_text(text, encoding="utf-8")

    jxl_cmake = src_dir / "lib" / "jxl.cmake"
    if jxl_cmake.exists():
        original_text = jxl_cmake.read_text(encoding="utf-8")
        text = original_text
        marker = "# OIIO_BUILDER_PUBLIC_JXL_INCLUDE_ORDER"
        if marker not in text:
            text = text.replace(
                """\
add_library(jxl_base INTERFACE)
target_include_directories(jxl_base BEFORE INTERFACE
  "$<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>"
)
target_include_directories(jxl_base BEFORE INTERFACE
  ${PROJECT_SOURCE_DIR}
  ${JXL_HWY_INCLUDE_DIRS}
)
""",
                """\
add_library(jxl_base INTERFACE)
# OIIO_BUILDER_PUBLIC_JXL_INCLUDE_ORDER
target_include_directories(jxl_base BEFORE INTERFACE
  ${PROJECT_SOURCE_DIR}
  ${JXL_HWY_INCLUDE_DIRS}
)
target_include_directories(jxl_base BEFORE INTERFACE
  "$<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>"
)
""",
                1,
            )
            text = text.replace(
                """\
target_include_directories(jxl_dec-obj BEFORE PUBLIC
  "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}>"
  "${JXL_HWY_INCLUDE_DIRS}"
  "$<BUILD_INTERFACE:$<TARGET_PROPERTY:brotlicommon,INTERFACE_INCLUDE_DIRECTORIES>>"
)
""",
                """\
target_include_directories(jxl_dec-obj BEFORE PUBLIC
  "$<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>"
  "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}>"
  "${JXL_HWY_INCLUDE_DIRS}"
  "$<BUILD_INTERFACE:$<TARGET_PROPERTY:brotlicommon,INTERFACE_INCLUDE_DIRECTORIES>>"
)
""",
                1,
            )
            text = text.replace(
                """\
target_include_directories(jxl_enc-obj BEFORE PUBLIC
  ${PROJECT_SOURCE_DIR}
  ${JXL_HWY_INCLUDE_DIRS}
  $<TARGET_PROPERTY:brotlicommon,INTERFACE_INCLUDE_DIRECTORIES>
)
""",
                """\
target_include_directories(jxl_enc-obj BEFORE PUBLIC
  "$<BUILD_INTERFACE:${CMAKE_CURRENT_BINARY_DIR}/include>"
  ${PROJECT_SOURCE_DIR}
  ${JXL_HWY_INCLUDE_DIRS}
  $<TARGET_PROPERTY:brotlicommon,INTERFACE_INCLUDE_DIRECTORIES>
)
""",
                1,
            )
        if text != original_text:
            jxl_cmake.write_text(text, encoding="utf-8")

    cmake_file = src_dir / "lib" / "jxl_extras.cmake"
    if cmake_file.exists():
        original_text = cmake_file.read_text(encoding="utf-8")
        text = original_text
        if "JXL_OPENEXR_STATIC_PATCH" not in text:
            text = text.replace(
                "list(APPEND JXL_EXTRAS_CODEC_INTERNAL_LIBRARIES PkgConfig::OpenEXR)",
                "# JXL_OPENEXR_STATIC_PATCH\n    list(APPEND JXL_EXTRAS_CODEC_INTERNAL_LIBRARIES PkgConfig::OpenEXR ${OpenEXR_STATIC_LIBRARIES})",
            )
            marker = "if (OpenEXR_FOUND)"
            if marker in text:
                insert = (
                    "if (OpenEXR_FOUND)\n"
                    "  # JXL_OPENEXR_STATIC_PATCH\n"
                    "  if (OpenEXR_STATIC_LIBRARIES AND TARGET PkgConfig::OpenEXR)\n"
                    "    set_property(TARGET PkgConfig::OpenEXR APPEND PROPERTY INTERFACE_LINK_LIBRARIES \"${OpenEXR_STATIC_LIBRARIES}\")\n"
                    "  endif()\n"
                    "  if (OpenEXR_LIBRARY_DIRS AND TARGET PkgConfig::OpenEXR)\n"
                    "    set_property(TARGET PkgConfig::OpenEXR APPEND PROPERTY INTERFACE_LINK_DIRECTORIES \"${OpenEXR_LIBRARY_DIRS}\")\n"
                    "  endif()\n"
                )
                text = text.replace(marker, insert, 1)
        if text != original_text:
            cmake_file.write_text(text, encoding="utf-8")

    third_party_cmake = src_dir / "third_party" / "CMakeLists.txt"
    if not third_party_cmake.exists():
        return
    original_text = third_party_cmake.read_text(encoding="utf-8")
    text = original_text
    begin = "# OIIO_BUILDER_BROTLI_FALLBACK_BEGIN"
    end = "# OIIO_BUILDER_BROTLI_FALLBACK_END"
    replacement = (
        "# OIIO_BUILDER_BROTLI_FALLBACK_BEGIN\n"
        "find_package(Brotli CONFIG QUIET)\n"
        "if(NOT Brotli_FOUND)\n"
        "  list(PREPEND CMAKE_MODULE_PATH \"${PROJECT_SOURCE_DIR}/cmake\")\n"
        "  find_package(Brotli REQUIRED)\n"
        "endif()\n"
        "# OIIO_BUILDER_BROTLI_FALLBACK_END"
    )
    if begin in text and end in text:
        start = text.index(begin)
        stop = text.index(end, start) + len(end)
        text = text[:start] + replacement + text[stop:]
    else:
        needle = "find_package(Brotli CONFIG REQUIRED)"
        if needle in text:
            text = text.replace(needle, replacement, 1)
        else:
            text = re.sub(
                r"find_package\(\s*Brotli\s+CONFIG\s+REQUIRED\s*\)",
                replacement,
                text,
                count=1,
            )
    if text != original_text:
        third_party_cmake.write_text(text, encoding="utf-8")
