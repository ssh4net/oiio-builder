from __future__ import annotations

import re

STAMP_REVISION = "5"


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    enable_openexr = "ON" if cfg.build_exr_stack else "OFF"
    args = [
        "-DBUILD_TESTING=OFF",
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
    return args


def patch_source(_builder, src_dir) -> None:
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
