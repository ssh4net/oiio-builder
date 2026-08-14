from __future__ import annotations

from pathlib import Path

STAMP_REVISION = "4"


def resolve_build_system(builder, _repo, src_dir) -> str | None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if builder.config.global_cfg.lcms2_use_autotools or not cmake_lists.exists():
        return "autotools"
    return "cmake"


def autotools_args(_builder, _repo) -> list[str]:
    return ["--without-fastfloat", "--without-threaded"]


def cmake_args(builder, _ctx) -> list[str]:
    # Little-CMS defaults to building BOTH variants. Build exactly the linkage
    # selected by the prefix to avoid duplicate downstream links on Windows.
    static_linkage = bool(builder.config.global_cfg.static_default)
    return [
        f"-DLCMS2_BUILD_SHARED={'OFF' if static_linkage else 'ON'}",
        f"-DLCMS2_BUILD_STATIC={'ON' if static_linkage else 'OFF'}",
        "-DLCMS2_BUILD_TOOLS=OFF",
        "-DLCMS2_BUILD_TESTS=OFF",
        "-DLCMS2_BUILD_JPGICC=OFF",
        "-DLCMS2_BUILD_TIFICC=OFF",
        "-DLCMS2_WITH_JPEG=OFF",
        "-DLCMS2_WITH_TIFF=OFF",
        "-DLCMS2_WITH_ZLIB=OFF",
    ]


def post_install(builder, install_prefix, _build_type: str) -> None:
    if builder.config.global_cfg.static_default:
        builder._prune_lcms2_shared_artifacts(install_prefix)
    _remove_stale_windows_cmake_package(builder, install_prefix)


def _remove_stale_windows_cmake_package(builder, install_prefix: Path) -> None:
    if builder.dry_run or builder.platform.os != "windows":
        return

    cmake_dir = install_prefix / "lib" / "cmake" / "lcms2"
    if not (cmake_dir / "lcms2-config.cmake").exists():
        return

    # Current Little-CMS installs lower-case package files. Older installs left
    # capitalized files in the same directory; find_package(lcms2) may pick
    # those first, and they can reference stale libraries such as lcms2.lib.
    for pattern in ("lcms2Config*.cmake", "lcms2Targets*.cmake"):
        for path in cmake_dir.glob(pattern):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
