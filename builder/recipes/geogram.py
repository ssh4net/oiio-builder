from __future__ import annotations

from pathlib import Path

from ._geometry_support import initialize_missing_submodules, patch_cmake_block

STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_geogram)


def cmake_args(builder, ctx) -> list[str]:
    shared = getattr(ctx.repo, "shared", None)
    shared = not builder.config.global_cfg.static_default if shared is None else bool(shared)
    if builder.platform.os == "windows":
        platform = "Win-vs-dynamic-generic" if shared else "Win-vs-generic"
    elif builder.platform.os == "macos":
        platform = ("Darwin-aarch64-clang" if builder.platform.arch == "arm64" else "Darwin-clang")
        platform += "-dynamic" if shared else ""
    else:
        compiler = str(builder.toolchain.get("cxx", "")).lower()
        family = "clang" if "clang" in compiler else "gcc"
        arch = "" if builder.platform.arch == "x86_64" else "nonx86-"
        platform = f"Linux64-{arch}{family}" + ("-dynamic" if shared or arch else "")
    return [f"-DVORPALINE_PLATFORM={platform}", f"-DVORPALINE_BUILD_DYNAMIC={'ON' if shared else 'OFF'}"]


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return
    patch_cmake_block(src_dir / "cmake" / "geogram.cmake", "GEOGRAM_LINKAGE",
                      "if(DEFINED BUILD_SHARED_LIBS)\n"
                      "  set(VORPALINE_BUILD_DYNAMIC ${BUILD_SHARED_LIBS})\nendif()",
                      before="# Static versus dynamic builds")
    patch_cmake_block(src_dir / "CMakeLists.txt", "GEOGRAM_DEPENDENCIES",
                      "find_package(Threads REQUIRED)\n"
                      "if(GEOGRAM_WITH_GRAPHICS)\n"
                      "  find_package(glfw3 CONFIG REQUIRED)\n"
                      "  find_package(OpenGL REQUIRED)\n"
                      "  if(UNIX AND NOT APPLE AND NOT EMSCRIPTEN)\n"
                      "    find_package(X11 REQUIRED)\n  endif()\nendif()\n"
                      "if((UNIX AND NOT APPLE AND NOT GEOGRAM_WITH_EMSCRIPTEN) OR GEOGRAM_WITH_TBB)\n"
                      "  find_package(TBB CONFIG REQUIRED)\nendif()",
                      before="if(GEOGRAM_WITH_TBB AND NOT TARGET TBB::tbb)")
    block = (Path(__file__).parent / "cmake" / "geogram-install.cmake").read_text(encoding="utf-8")
    patch_cmake_block(src_dir / "CMakeLists.txt", "GEOGRAM_PACKAGE", block)


def pre_build(builder, _repo, ctx, env: dict[str, str]) -> None:
    initialize_missing_submodules(builder, ctx, env)
