from __future__ import annotations

STAMP_REVISION = "1"


def patch_source(_builder, src_dir) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if not cmake_lists.exists():
        return

    original_text = cmake_lists.read_text(encoding="utf-8")
    text = original_text

    # libultrahdr forces CMP0091 to OLD, which prevents CMake from honoring
    # CMAKE_MSVC_RUNTIME_LIBRARY and leads to `/MD(d)` libraries being produced
    # even when the build uses `/MT(d)`.
    marker = "# oiio-builder: enforce CMP0091 NEW"
    if marker not in text and "cmake_policy(SET CMP0091 OLD)" in text:
        text = text.replace(
            "cmake_policy(SET CMP0091 OLD)",
            f"{marker}\n  cmake_policy(SET CMP0091 NEW)",
            1,
        )

    if text != original_text:
        cmake_lists.write_text(text, encoding="utf-8")

