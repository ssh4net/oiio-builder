from __future__ import annotations

import re

STAMP_REVISION = "3"


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

    cmake_file = src_dir / "src" / "wrappers" / "python" / "CMakeLists.txt"
    if not cmake_file.exists():
        return
    text = cmake_file.read_text(encoding="utf-8")
    begin = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN"
    end = "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END"
    replacement = (
        "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN\n"
        "target_link_libraries (PyOpenEXR PRIVATE OpenEXR::OpenEXR pybind11::headers)\n"
        "if(TARGET Python3::Module)\n"
        "  target_link_libraries (PyOpenEXR PRIVATE Python3::Module)\n"
        "elseif(TARGET Python3::Python)\n"
        "  target_link_libraries (PyOpenEXR PRIVATE Python3::Python)\n"
        "else()\n"
        "  target_link_libraries (PyOpenEXR PRIVATE ${Python3_LIBRARIES})\n"
        "endif()\n"
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
