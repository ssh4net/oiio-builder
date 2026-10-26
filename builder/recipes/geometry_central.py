from __future__ import annotations

from pathlib import Path

from ._geometry_support import initialize_missing_submodules, patch_cmake_block

STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_geometry_central)


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return
    patch_cmake_block(src_dir / "CMakeLists.txt", "GEOMETRY_CENTRAL_EIGEN",
                      "find_package(Eigen3 3.3 CONFIG REQUIRED)\nfind_package(Threads REQUIRED)",
                      before="add_subdirectory(deps)")
    block = (Path(__file__).parent / "cmake" / "geometry-central-install.cmake").read_text(encoding="utf-8")
    patch_cmake_block(src_dir / "CMakeLists.txt", "GEOMETRY_CENTRAL_PACKAGE", block)
    # Avoid a Windows symlink privilege requirement for a compatibility header.
    deps = src_dir / "deps" / "CMakeLists.txt"
    with deps.open(encoding="utf-8", newline="") as source:
        text = source.read()
    updated = text.replace("nanoflann.hpp SYMBOLIC)", "nanoflann.hpp SYMBOLIC COPY_ON_ERROR)", 1)
    updated = updated.replace('nanoflann.hpp" SYMBOLIC)', 'nanoflann.hpp" SYMBOLIC COPY_ON_ERROR)', 1)
    if updated != text:
        with deps.open("w", encoding="utf-8", newline="") as source:
            source.write(updated)


def pre_build(builder, _repo, ctx, env: dict[str, str]) -> None:
    initialize_missing_submodules(builder, ctx, env)
