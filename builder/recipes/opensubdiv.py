from __future__ import annotations

from pathlib import Path

from ..tooling import resolve_openmp_root
from ._geometry_support import patch_cmake_block


def enabled(builder, _repo) -> bool:
    return builder.config.global_cfg.build_opensubdiv


def cmake_args(builder, _ctx) -> list[str]:
    cfg = builder.config.global_cfg
    modules = Path(__file__).parent / "cmake" / "opensubdiv"
    args = [f"-DCMAKE_MODULE_PATH={modules.as_posix()}",
            f"-DNO_PTEX={'OFF' if cfg.build_ptex else 'ON'}"]
    env = dict(cfg.env)
    if builder.platform.os == "windows":
        env.update(cfg.windows_env)
        static_crt = builder._windows_runtime_mode() == "static"
        args.append(f"-DMSVC_STATIC_CRT={'ON' if static_crt else 'OFF'}")
    omp_root = resolve_openmp_root(env, platform_os=builder.platform.os)
    if omp_root:
        args.append(f"-DOpenMP_ROOT={omp_root}")
    return args


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return
    # The object library and the exported libraries both inherit this list.
    patch_cmake_block(src_dir / "CMakeLists.txt", "OPENSUBDIV_OPENMP",
                      "if(OpenMP_CXX_FOUND AND NOT NO_OMP)\n"
                      "  list(APPEND PLATFORM_CPU_LIBRARIES OpenMP::OpenMP_CXX)\nendif()",
                      before="add_subdirectory(opensubdiv)")
    patch_cmake_block(src_dir / "opensubdiv-config.cmake.in", "OPENSUBDIV_DEPENDENCIES",
                      "include(CMakeFindDependencyMacro)\n"
                      "if(@OpenMP_CXX_FOUND@)\n"
                      "  find_dependency(OpenMP COMPONENTS CXX)\nendif()\n"
                      "if(@TBB_FOUND@)\n"
                      "  find_dependency(TBB CONFIG COMPONENTS tbb)\nendif()",
                      before='include("${CMAKE_CURRENT_LIST_DIR}/OpenSubdivTargets.cmake")')
