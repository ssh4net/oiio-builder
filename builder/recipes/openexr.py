from __future__ import annotations

import re

from .policy import exr_enabled

STAMP_REVISION = "5"


def enabled(builder, _repo) -> bool:
    return exr_enabled(builder)


def cmake_args(builder, _ctx) -> list[str]:
    openexr_build_python = "ON"
    if builder.platform.os == "windows":
        wrappers_enabled, reason = builder._windows_python_wrappers_enabled()
        openexr_build_python = "ON" if wrappers_enabled else "OFF"
        if openexr_build_python == "OFF" and not builder._openexr_python_note_printed:
            if reason == "forced-off":
                print("[note] OpenEXR: OPENEXR_BUILD_PYTHON=OFF (windows.python_wrappers=off)", flush=True)
            else:
                print(
                    "[note] OpenEXR: OPENEXR_BUILD_PYTHON=OFF (windows.python_wrappers=auto with static CRT). "
                    "Set windows.python_wrappers=on (or windows.msvc_runtime=dynamic) to enable wrappers.",
                    flush=True,
                )
            builder._openexr_python_note_printed = True
    return [
        "-DOPENEXR_BUILD_TOOLS=ON",
        "-DOPENEXR_INSTALL_TOOLS=ON",
        "-DOPENEXR_BUILD_EXAMPLES=ON",
        "-DOPENEXR_BUILD_TESTS=OFF",
        f"-DOPENEXR_BUILD_PYTHON={openexr_build_python}",
        "-DOPENEXR_TEST_PYTHON=OFF",
        "-DBUILD_TESTING=OFF",
        "-DOPENEXR_FORCE_INTERNAL_IMATH=OFF",
        "-DOPENEXR_FORCE_INTERNAL_DEFLATE=OFF",
        "-DOPENEXR_FORCE_INTERNAL_OPENJPH=OFF",
        "-DCMAKE_SKIP_RPATH=ON",
        "-DCMAKE_SKIP_INSTALL_RPATH=ON",
    ]


def patch_source(builder, src_dir) -> None:
    if builder.platform.os != "windows":
        return

    # clang-cl defines _MSC_VER but still requires explicit -m* flags for
    # SSSE3/SSE4.1 intrinsics (otherwise clang errors on always_inline intrinsics).
    core_cmake = src_dir / "src" / "lib" / "OpenEXRCore" / "CMakeLists.txt"
    openexr_cmake = src_dir / "src" / "lib" / "OpenEXR" / "CMakeLists.txt"

    begin_simd = "# OIIO_BUILDER_CLANGCL_SIMD_BEGIN"
    end_simd = "# OIIO_BUILDER_CLANGCL_SIMD_END"
    simd_block_core = (
        f"{begin_simd}\n"
        "# clang-cl: enable SSE4.1 intrinsics in OpenEXRCore/internal_zip.c\n"
        'if(MSVC AND CMAKE_C_COMPILER_ID MATCHES "Clang")\n'
        '  set_source_files_properties(internal_zip.c PROPERTIES COMPILE_FLAGS "-msse4.1")\n'
        "endif()\n"
        f"{end_simd}\n"
    )
    simd_block_openexr = (
        f"{begin_simd}\n"
        "# clang-cl: enable SSE4.1 intrinsics in OpenEXR/ImfZip.cpp\n"
        'if(MSVC AND CMAKE_CXX_COMPILER_ID MATCHES "Clang")\n'
        '  set_source_files_properties(ImfZip.cpp PROPERTIES COMPILE_FLAGS "-msse4.1")\n'
        "endif()\n"
        f"{end_simd}\n"
    )

    if core_cmake.exists():
        text = core_cmake.read_text(encoding="utf-8", errors="replace")
        if begin_simd not in text:
            core_cmake.write_text(text + "\n" + simd_block_core, encoding="utf-8")

    if openexr_cmake.exists():
        text = openexr_cmake.read_text(encoding="utf-8", errors="replace")
        if begin_simd not in text:
            openexr_cmake.write_text(text + "\n" + simd_block_openexr, encoding="utf-8")

    # OpenEXR 4 declares KeyCode::operator== without IMF_EXPORT, so it is
    # omitted from Windows DLL import libraries even though PyOpenEXR uses it.
    keycode_header = src_dir / "src" / "lib" / "OpenEXR" / "ImfKeyCode.h"
    if keycode_header.exists():
        text = keycode_header.read_text(encoding="utf-8", errors="replace")
        declaration = "    bool operator== (const KeyCode& other) const;"
        exported_declaration = f"    IMF_EXPORT\n{declaration}"
        if exported_declaration not in text and declaration in text:
            keycode_header.write_text(
                text.replace(declaration, exported_declaration, 1),
                encoding="utf-8",
            )

    cmake_file = src_dir / "src" / "wrappers" / "python" / "CMakeLists.txt"
    if not cmake_file.exists():
        return
    text = cmake_file.read_text(encoding="utf-8")
    begin = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN"
    end = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END"
    replacement = (
        "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN\n"
        "target_link_libraries (PyOpenEXR PRIVATE OpenEXR::OpenEXR pybind11::module)\n"
        "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END"
    )
    if begin in text and end in text:
        start = text.index(begin)
        stop = text.index(end, start) + len(end)
        text = text[:start] + replacement + text[stop:]
    else:
        pattern = r'target_link_libraries\s*\(\s*PyOpenEXR\s+PRIVATE\s+"?\$\{Python3_LIBRARIES\}"?\s+OpenEXR::OpenEXR\s+pybind11::headers\s*\)'
        if re.search(pattern, text):
            text = re.sub(pattern, replacement, text, count=1)
    cmake_file.write_text(text, encoding="utf-8")


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._make_openexr_pc_override(install_prefix, build_type)
