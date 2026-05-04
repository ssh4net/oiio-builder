from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "1"


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return

    cmake_file = src_dir / "CMakeLists.txt"
    if not cmake_file.exists():
        return

    text = cmake_file.read_text(encoding="utf-8", errors="replace")
    changed = False

    option_marker = "# OIIO_BUILDER_MINIPLY_TOOLS_OPTION"
    if option_marker not in text:
        needle = "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n"
        replacement = (
            "set(CMAKE_CXX_STANDARD_REQUIRED ON)\n\n"
            f"{option_marker}\n"
            'option(MINIPLY_BUILD_TOOLS "Build miniply command line tools." ON)\n'
        )
        if needle in text:
            text = text.replace(needle, replacement, 1)
            changed = True

    tools_marker = "# OIIO_BUILDER_MINIPLY_TOOLS_BEGIN"
    if tools_marker not in text:
        old = """add_executable(miniply-perf
  miniply.cpp
  miniply.h
  extra/miniply-perf.cpp
)

add_executable(miniply-info
  miniply.cpp
  miniply.h
  extra/miniply-info.cpp
)

"""
        new = """# OIIO_BUILDER_MINIPLY_TOOLS_BEGIN
if(MINIPLY_BUILD_TOOLS)
  add_executable(miniply-perf
    miniply.cpp
    miniply.h
    extra/miniply-perf.cpp
  )

  add_executable(miniply-info
    miniply.cpp
    miniply.h
    extra/miniply-info.cpp
  )
endif()
# OIIO_BUILDER_MINIPLY_TOOLS_END

"""
        if old in text:
            text = text.replace(old, new, 1)
            changed = True

    if changed:
        cmake_file.write_text(text, encoding="utf-8")
