from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "1"


def patch_source(_builder, src_dir: Path) -> None:
    flags_cmake = src_dir / "build" / "cmake" / "CMakeModules" / "AddZstdCompilationFlags.cmake"
    if not flags_cmake.exists():
        return

    original_text = flags_cmake.read_text(encoding="utf-8", errors="replace")
    text = original_text

    text = text.replace("if(CMAKE_CXX_COMPILER)\n", "if(CMAKE_CXX_COMPILER_LOADED)\n")
    text = text.replace(
        "    if (_CXX AND CMAKE_CXX_COMPILER)\n",
        "    if (_CXX AND CMAKE_CXX_COMPILER_LOADED)\n",
    )

    if text != original_text:
        flags_cmake.write_text(text, encoding="utf-8")
