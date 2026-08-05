from __future__ import annotations

import shutil
from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "2"


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


def post_install(builder, install_prefix: Path, _build_type: str) -> None:
    if builder.platform.os != "windows" or builder.dry_run:
        return

    # gif_lib.h includes this public compatibility header under _WIN32, but
    # giflib's CMake install(FILES ...) list omits it.
    source_dir = builder.repo_paths.get("giflib")
    header_source = source_dir / "gif_win32_compat.h" if source_dir is not None else None
    if header_source is None or not header_source.is_file():
        raise RuntimeError("giflib Windows compatibility header is missing from the source checkout")

    include_dir = install_prefix / "include"
    include_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(header_source, include_dir / header_source.name)
