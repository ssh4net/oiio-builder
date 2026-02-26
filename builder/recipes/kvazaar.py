from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_kvazaar)


def patch_source(_builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_text(encoding="utf-8", errors="replace")
    text = original_text

    marker = "# oiio-builder: clang-cl needs -msse4.1 for SSE4.1 strategy sources"
    if marker not in text:
        needle = 'set_property( SOURCE ${LIB_SOURCES_STRATEGIES_AVX2} APPEND PROPERTY COMPILE_FLAGS "/arch:AVX2" )'
        if needle in text:
            replacement = (
                f'{needle}\n'
                f'    {marker}\n'
                '    if(CMAKE_C_COMPILER_ID MATCHES "Clang")\n'
                '      set_property( SOURCE ${LIB_SOURCES_STRATEGIES_SSE41} APPEND PROPERTY COMPILE_FLAGS "-msse4.1" )\n'
                "    endif()\n"
            )
            text = text.replace(needle, replacement, 1)

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")
