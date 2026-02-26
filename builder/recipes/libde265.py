from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "3"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_libde265)


def patch_source(_builder, src_dir: Path) -> None:
    """Fix clang-cl builds by ensuring SSE4.1 sources get the right target flags.

    Upstream treats `MSVC` as a proxy for `cl.exe` and therefore avoids using
    `-msse4.1`. But clang-cl defines `MSVC=ON` while still accepting clang-style
    `-m...` target feature flags, and it may require them for SSE4.1 intrinsics.
    """

    util_h = src_dir / "libde265" / "util.h"
    if util_h.exists():
        text = util_h.read_text(encoding="utf-8", errors="replace")
        original = "#if defined(_MSC_VER) || (!__clang__ && __GNUC__ && GCC_VERSION < 40600)\n"
        if original in text:
            patched = text.replace(
                original,
                "#if (defined(_MSC_VER) && !defined(__clang__)) || (!__clang__ && __GNUC__ && GCC_VERSION < 40600)\n",
                1,
            )
            util_h.write_text(patched, encoding="utf-8")

    cmake_lists = src_dir / "libde265" / "x86" / "CMakeLists.txt"
    if cmake_lists.exists():
        text = cmake_lists.read_text(encoding="utf-8", errors="replace")
        if 'CMAKE_CXX_COMPILER_ID MATCHES "Clang"' not in text:
            original = "if(NOT MSVC)\n"
            if original in text:
                patched = text.replace(
                    original,
                    "# clang-cl defines MSVC=ON but still supports -m... target feature flags.\n"
                    'if(NOT MSVC OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")\n',
                    1,
                )
                cmake_lists.write_text(patched, encoding="utf-8")

    getopt_long_c = src_dir / "extra" / "getopt_long.c"
    if getopt_long_c.exists():
        text = getopt_long_c.read_text(encoding="utf-8", errors="replace")
        # Match the existing prototype to avoid clang errors under clang-cl.
        text2 = text.replace(
            "getopt_internal(int nargc, char ** nargv, const char *ostr)",
            "getopt_internal(int nargc, char * const * nargv, const char *ostr)",
        )
        if text2 != text:
            getopt_long_c.write_text(text2, encoding="utf-8")
