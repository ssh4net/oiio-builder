from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "1"


def _patch_file(path: Path, *, needle: str, replacement: str, marker: str) -> None:
    if not path.exists():
        return

    original_text = path.read_text(encoding="utf-8", errors="replace")
    text = original_text

    if marker not in text and needle in text:
        text = text.replace(needle, replacement, 1)

    if text != original_text:
        path.write_text(text, encoding="utf-8")


def patch_source(_builder, src_dir: Path) -> None:
    # clang-cl reports missing CPU features for SSSE3/SSE4.1 intrinsics unless
    # the corresponding -m* flag is set, while MSVC typically accepts these
    # intrinsics without additional /arch gating.

    core_cmake = src_dir / "src" / "core" / "CMakeLists.txt"
    core_marker = "# oiio-builder: clang-cl needs -mssse3 for SSSE3 sources"
    _patch_file(
        core_cmake,
        needle='set_source_files_properties(transform/ojph_transform_avx512.cpp PROPERTIES COMPILE_FLAGS "/arch:AVX512")',
        replacement=(
            'set_source_files_properties(transform/ojph_transform_avx512.cpp PROPERTIES COMPILE_FLAGS "/arch:AVX512")\n'
            f"        {core_marker}\n"
            '        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")\n'
            '          set_source_files_properties(coding/ojph_block_decoder_ssse3.cpp PROPERTIES COMPILE_FLAGS "-mssse3")\n'
            "        endif()\n"
        ),
        marker=core_marker,
    )

    # Executables: enable SSE4.1 for clang-cl when MSVC is on.
    for app in ("ojph_expand", "ojph_compress"):
        app_cmake = src_dir / "src" / "apps" / app / "CMakeLists.txt"
        app_marker = "# oiio-builder: clang-cl needs -msse4.1 for SSE4.1 sources"
        _patch_file(
            app_cmake,
            needle='set_source_files_properties(${OJPH_IMG_IO_AVX2} PROPERTIES COMPILE_FLAGS "/arch:AVX2")',
            replacement=(
                'set_source_files_properties(${OJPH_IMG_IO_AVX2} PROPERTIES COMPILE_FLAGS "/arch:AVX2")\n'
                f"        {app_marker}\n"
                '        if(CMAKE_CXX_COMPILER_ID MATCHES "Clang")\n'
                '          set_source_files_properties(${OJPH_IMG_IO_SSE4} PROPERTIES COMPILE_FLAGS "-msse4.1")\n'
                "        endif()\n"
            ),
            marker=app_marker,
        )

