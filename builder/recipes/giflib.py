from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def patch_source(_builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_text(encoding="utf-8", errors="replace")
    text = original_text

    marker = "# oiio-builder: gifclrmp uses pow(), which needs libm on Unix"
    if marker not in text:
        needle = "        target_link_libraries(${UTIL} PRIVATE gif)\n"
        replacement = (
            needle +
            f"        {marker}\n"
            '        if(UNIX AND UTIL STREQUAL "gifclrmp")\n'
            "            target_link_libraries(${UTIL} PRIVATE m)\n"
            "        endif()\n"
        )
        text = text.replace(needle, replacement, 1)

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")
