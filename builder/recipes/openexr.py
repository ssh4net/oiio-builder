from __future__ import annotations

import re
from pathlib import Path

from .policy import exr_enabled

STAMP_REVISION = "7"


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
        "-DCMAKE_SKIP_RPATH=ON",
        "-DCMAKE_SKIP_INSTALL_RPATH=ON",
    ]


_SYSTEM_OPENJPH_CMAKE = """\
# OIIO_BUILDER_SYSTEM_OPENJPH_BEGIN
# Always use the installed package; never embed a second OpenJPH implementation.
if(OPENEXR_FORCE_INTERNAL_OPENJPH)
  message(FATAL_ERROR "oiio-builder requires installed OpenJPH; bundled OpenJPH is disabled")
endif()
find_package(openjph CONFIG REQUIRED)
if(NOT TARGET openjph)
  message(FATAL_ERROR "The installed OpenJPH CONFIG package must define target openjph")
endif()
if(NOT DEFINED openjph_VERSION OR "${openjph_VERSION}" STREQUAL "")
  message(FATAL_ERROR "The installed OpenJPH CONFIG package must report its version")
endif()
if(NOT "${openjph_VERSION}" MATCHES "^[0-9]+([.][0-9]+)?([.][0-9]+)?([.][0-9]+)?$")
  message(FATAL_ERROR "The installed OpenJPH CONFIG package must report a numeric version; found ${openjph_VERSION}")
endif()
set(_oiio_openjph_min_version "@MINIMUM_VERSION@")
if(openjph_VERSION VERSION_LESS _oiio_openjph_min_version)
  message(FATAL_ERROR "OpenEXR requires OpenJPH >= ${_oiio_openjph_min_version}; found ${openjph_VERSION}")
endif()
set(OIIO_BUILDER_OPENJPH_VERSION "${openjph_VERSION}")
set(EXR_OPENJPH_LIB openjph)
set(OPENEXR_USE_INTERNAL_OPENJPH 0 CACHE INTERNAL "Use installed OpenJPH" FORCE)
set(EXR_OPENJPH_PKGCONFIG_REQUIRES "openjph >= ${OIIO_BUILDER_OPENJPH_VERSION}")
message(STATUS "Using installed OpenJPH ${OIIO_BUILDER_OPENJPH_VERSION} from ${openjph_DIR}")
unset(_oiio_openjph_min_version)
# OIIO_BUILDER_SYSTEM_OPENJPH_END
"""


def _patch_system_openjph(src_dir: Path) -> None:
    setup = src_dir / "cmake" / "OpenEXRSetup.cmake"
    if not setup.exists():
        return
    with setup.open(encoding="utf-8", newline="") as source:
        original = source.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    block = re.search(
        r"^# OIIO_BUILDER_SYSTEM_OPENJPH_BEGIN\n.*?^# OIIO_BUILDER_SYSTEM_OPENJPH_END\n?(?:[ \t]*\n)*",
        text, re.MULTILINE | re.DOTALL,
    )
    if block is not None:
        minimum = re.search(r'set\(_oiio_openjph_min_version "([0-9]+(?:\.[0-9]+)*)"\)', block.group())
        if minimum is None:
            raise RuntimeError(f"Cannot determine OpenEXR's OpenJPH requirement: {setup}")
    else:
        # Section boundaries let this replace the complete upstream fallback,
        # including the former version-specific builder workaround.
        block = re.search(
            r"^#{3,}\n# Find or (?:download|install) OpenJPH\n#{3,}\n"
            r".*?(?=^#{3,}\n# )", text, re.MULTILINE | re.DOTALL,
        )
        if block is None:
            if re.search(r"(?:find_package|pkg_check_modules)\s*\(\s*openjph\b", text, re.IGNORECASE):
                raise RuntimeError(f"OpenEXR system OpenJPH patch no longer matches upstream source: {setup}")
            return  # Older OpenEXR without an OpenJPH dependency.
        minimum = re.search(r"find_package\s*\(\s*openjph\s+([0-9]+(?:\.[0-9]+)*)\b", block.group())
        if minimum is None:
            raise RuntimeError(f"Cannot determine OpenEXR's OpenJPH requirement: {setup}")
    minimum_version = minimum.group(1)
    replacement = _SYSTEM_OPENJPH_CMAKE.replace("@MINIMUM_VERSION@", minimum_version)
    updated_setup = text[:block.start()] + replacement + "\n" + text[block.end():]

    config = src_dir / "cmake" / "OpenEXRConfig.cmake.in"
    if not config.exists():
        raise RuntimeError(f"OpenEXR system OpenJPH export patch target is missing: {config}")
    with config.open(encoding="utf-8", newline="") as source:
        original_config = source.read()
    config_text = original_config.replace("\r\n", "\n")
    dependency = re.search(
        r'^if\s*\("@EXR_OPENJPH_LIB@"[^\n]*\n.*?^endif\s*\(\s*\)\n?',
        config_text, re.MULTILINE | re.DOTALL,
    )
    if dependency is None:
        dependency = re.search(
            r"(?:^# OIIO_BUILDER_SYSTEM_OPENJPH_DEPENDENCY\n)?"
            r"^find_dependency\(openjph[^\n]*\)\n?", config_text, re.MULTILINE,
        )
    if dependency is None:
        raise RuntimeError(f"OpenEXR system OpenJPH export patch no longer matches upstream source: {config}")
    export = ("# OIIO_BUILDER_SYSTEM_OPENJPH_DEPENDENCY\n"
              "find_dependency(openjph @OIIO_BUILDER_OPENJPH_VERSION@ CONFIG)\n")
    updated_config = config_text[:dependency.start()] + export + config_text[dependency.end():]
    # Validate both transformations before writing either file.
    if updated_setup != text:
        with setup.open("w", encoding="utf-8", newline="") as source:
            source.write(updated_setup.replace("\n", newline))
    if updated_config != config_text:
        with config.open("w", encoding="utf-8", newline="") as source:
            source.write(updated_config.replace("\n", "\r\n" if "\r\n" in original_config else "\n"))


def patch_source(builder, src_dir) -> None:
    _patch_system_openjph(src_dir)
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
