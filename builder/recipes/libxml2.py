from __future__ import annotations


def cmake_args(builder, ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []
    if any(a.startswith("-DLIBXML2_WITH_ICONV=") for a in ctx.repo.cmake_args):
        return []

    cfg = builder.config.global_cfg
    debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
    iconv_header = ctx.install_prefix / "include" / "iconv.h"
    iconv_cfg = ctx.install_prefix / "lib" / "cmake" / "Iconv" / "IconvConfig.cmake"
    if ctx.build_type == "Debug":
        iconv_lib = ctx.install_prefix / "lib" / f"iconv{debug_postfix}.lib"
    else:
        iconv_lib = ctx.install_prefix / "lib" / "iconv.lib"

    if iconv_cfg.exists() or (iconv_header.exists() and iconv_lib.exists()):
        return ["-DLIBXML2_WITH_ICONV=ON"]
    return ["-DLIBXML2_WITH_ICONV=OFF"]
