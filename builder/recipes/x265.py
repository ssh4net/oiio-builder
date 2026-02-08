from __future__ import annotations

from pathlib import Path

STAMP_REVISION = "1"


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []

    runtime_mode = str(builder.config.global_cfg.windows.get("msvc_runtime", "static")).strip().lower()
    if runtime_mode in {"", "static", "mt", "multithreaded"}:
        return ["-DSTATIC_LINK_CRT=ON"]
    if runtime_mode in {"dynamic", "md", "multithreadeddll"}:
        return ["-DSTATIC_LINK_CRT=OFF"]
    return []


def patch_source(_builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_text(encoding="utf-8")
    text = original_text

    marker = "# oiio-builder: STATIC_LINK_CRT all configs"
    if marker not in text:
        needle = "set(CompilerFlags CMAKE_CXX_FLAGS_RELEASE CMAKE_C_FLAGS_RELEASE)"
        replacement = (
            f"{marker}\n"
            "        set(CompilerFlags"
            " CMAKE_CXX_FLAGS_RELEASE CMAKE_C_FLAGS_RELEASE"
            " CMAKE_CXX_FLAGS_RELWITHDEBINFO CMAKE_C_FLAGS_RELWITHDEBINFO"
            " CMAKE_CXX_FLAGS_MINSIZEREL CMAKE_C_FLAGS_MINSIZEREL"
            " CMAKE_CXX_FLAGS_DEBUG CMAKE_C_FLAGS_DEBUG)"
        )
        if needle in text:
            text = text.replace(needle, replacement, 1)

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")
