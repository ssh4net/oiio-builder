from __future__ import annotations

STAMP_REVISION = "3"


def resolve_build_system(builder, _repo, src_dir) -> str | None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if builder.config.global_cfg.lcms2_use_autotools or not cmake_lists.exists():
        return "autotools"
    return "cmake"


def autotools_args(_builder, _repo) -> list[str]:
    return ["--without-fastfloat", "--without-threaded"]


def cmake_args(_builder, _ctx) -> list[str]:
    # Little-CMS defaults to building BOTH shared and static libraries. For a
    # static OpenImageIO prefix, build only static to avoid linking both
    # variants into downstream targets (LNK2005/LNK1169).
    return [
        "-DLCMS2_BUILD_SHARED=OFF",
        "-DLCMS2_BUILD_STATIC=ON",
        "-DLCMS2_BUILD_TOOLS=OFF",
        "-DLCMS2_BUILD_TESTS=OFF",
        "-DLCMS2_BUILD_JPGICC=OFF",
        "-DLCMS2_BUILD_TIFICC=OFF",
        "-DLCMS2_WITH_JPEG=OFF",
        "-DLCMS2_WITH_TIFF=OFF",
        "-DLCMS2_WITH_ZLIB=OFF",
    ]


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._prune_lcms2_shared_artifacts(install_prefix)
