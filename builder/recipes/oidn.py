from __future__ import annotations

import os
import re
from pathlib import Path
from subprocess import CalledProcessError

from ..runner import run

STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_oidn)


def build_env(builder, _repo, build_type: str, _prefix: Path, env: dict[str, str]) -> None:
    if builder.platform.os not in {"linux", "macos"} or build_type != "ASAN":
        return
    # OIDN queries Python at configure time and runs it to embed model weights.
    # Match CPython's build-time leak policy without changing runtime defaults.
    asan_options = env.get("ASAN_OPTIONS", os.environ.get("ASAN_OPTIONS", ""))
    if not re.search(r"(?:^|[:\s])detect_leaks=", asan_options):
        env["ASAN_OPTIONS"] = ":".join(filter(None, (asan_options, "detect_leaks=0")))


def cmake_args(builder, _ctx) -> list[str]:
    static = builder.config.global_cfg.static_default
    args = [
        f"-DOIDN_STATIC_LIB={'ON' if static else 'OFF'}",
        # The patched option computes its default from the effective device options.
        f"-DOIDN_BUILDER_STATIC_CUDA={'ON' if static else 'OFF'}",
    ]
    if builder.platform.os == "macos" and builder.platform.arch != "arm64":
        # OIDN's Metal device supports Apple Silicon; Intel Macs use its CPU device.
        args.extend(["-DOIDN_DEVICE_METAL=OFF", "-DOIDN_DEVICE_CPU=ON"])
    return args


# Narrow, reversible edits for upstream v2.5.1. Validate every anchor before
# writing any files, so source drift cannot leave a partially applied patch.
_EDITS: dict[str, list[tuple[str, str]]] = {
    "CMakeLists.txt": [(
        "# Filters\n",
        '''# OIIO_BUILDER_STATIC_CUDA
option(OIDN_BUILDER_STATIC_CUDA "Enable the oiio-builder static CUDA path." OFF)
if(OIDN_BUILDER_STATIC_CUDA AND OIDN_STATIC_LIB AND OIDN_DEVICE_CUDA)
  if(OIDN_DEVICE_SYCL OR OIDN_DEVICE_HIP)
    message(FATAL_ERROR "Static CUDA cannot be combined with shared SYCL/HIP devices")
  endif()
  set(OIDN_STATIC_CUDA ON)
else()
  set(OIDN_STATIC_CUDA OFF)
endif()

# Filters
''')],
    "core/CMakeLists.txt": [(
        "if(OIDN_STATIC_LIB AND NOT OIDN_DEVICE_SYCL AND NOT OIDN_DEVICE_CUDA AND NOT OIDN_DEVICE_HIP)",
        "if(OIDN_STATIC_LIB AND NOT OIDN_DEVICE_SYCL AND (NOT OIDN_DEVICE_CUDA OR OIDN_STATIC_CUDA) AND NOT OIDN_DEVICE_HIP)")],
    "include/OpenImageDenoise/config.h.in": [(
        "#cmakedefine OIDN_STATIC_LIB\n",
        "#cmakedefine OIDN_STATIC_LIB\n#cmakedefine OIDN_STATIC_CUDA\n")],
    "core/context.h": [(
        "  OIDN_DECLARE_INIT_STATIC_MODULE(device_metal);",
        "  OIDN_DECLARE_INIT_STATIC_MODULE(device_metal);\n#if defined(OIDN_STATIC_CUDA)\n  OIDN_DECLARE_INIT_STATIC_MODULE(device_cuda);\n#endif"), (
        "        OIDN_INIT_MODULE(device_cuda);",
        "      {\n      #if defined(OIDN_STATIC_CUDA)\n        OIDN_INIT_STATIC_MODULE(device_cuda);\n      #else\n        OIDN_INIT_MODULE(device_cuda);\n      #endif\n      }")],
    "devices/cuda/cuda_module.cpp": [(
        "  OIDN_DECLARE_INIT_MODULE(device_cuda)",
        "#if defined(OIDN_STATIC_CUDA)\n  OIDN_DECLARE_INIT_STATIC_MODULE(device_cuda)\n#else\n  OIDN_DECLARE_INIT_MODULE(device_cuda)\n#endif")],
    "devices/CMakeLists.txt": [(
        "  mark_as_advanced(OIDN_DEVICE_CUDA_API)\n",
        '''  mark_as_advanced(OIDN_DEVICE_CUDA_API)

  if(OIDN_STATIC_CUDA)
    if(NOT OIDN_DEVICE_CUDA_API STREQUAL "RuntimeStatic")
      message(FATAL_ERROR "Static CUDA requires OIDN_DEVICE_CUDA_API=RuntimeStatic to avoid CURTN symbol collisions")
    endif()
    add_subdirectory(cuda)
  else()
'''), (
        "endif()\n\nif(OIDN_DEVICE_HIP)",
        "  endif() # shared CUDA external project\nendif()\n\nif(OIDN_DEVICE_HIP)")],
    "devices/cuda/CMakeLists.txt": [(
        "find_package(CUDAToolkit 12.8 REQUIRED)",
        '''if(OIDN_STATIC_CUDA AND CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64|amd64" AND
   CMAKE_CXX_FLAGS MATCHES "-stdlib=libc[+][+]")
  message(FATAL_ERROR "NVCC does not support libc++ on x86_64. Use a separate libstdc++ prefix for CUDA OIDN; keep the existing prefix ABI unchanged.")
endif()
find_package(CUDAToolkit 12.8 REQUIRED)'''), (
        "# Set common paths\n",
        "if(NOT OIDN_STATIC_CUDA)\n# Set common paths\n"), (
        "# CUDA\n",
        "endif()\n\n# CUDA\n"), (
        "enable_language(CUDA)",
        '''if(OIDN_STATIC_CUDA AND NOT DEFINED CMAKE_CUDA_HOST_COMPILER AND NOT WIN32)
  set(CMAKE_CUDA_HOST_COMPILER "${CMAKE_CXX_COMPILER}")
endif()
enable_language(CUDA)'''), (
        "include(oidn_common_external)",
        "if(NOT OIDN_STATIC_CUDA)\n  include(oidn_common_external)\nendif()"), (
        "add_library(OpenImageDenoise_device_cuda SHARED",
        '''if(OIDN_STATIC_CUDA)
  set(_oidn_cuda_lib_type STATIC)
else()
  set(_oidn_cuda_lib_type SHARED)
endif()
add_library(OpenImageDenoise_device_cuda ${_oidn_cuda_lib_type}'''), (
        "oidn_strip_symbols(OpenImageDenoise_device_cuda)\noidn_install_module(OpenImageDenoise_device_cuda)",
        '''if(OIDN_STATIC_CUDA)
  # Installed C++ consumers do not enable the CUDA language. Export its runtime
  # dependency explicitly instead of relying on CUDA's implicit linker flags.
  target_link_libraries(OpenImageDenoise_device_cuda PRIVATE CUDA::cudart_static)
  if(CMAKE_CXX_COMPILER_ID MATCHES "Clang" AND CMAKE_CXX_FLAGS MATCHES "-stdlib=libc[+][+]")
    target_compile_options(OpenImageDenoise_device_cuda PRIVATE
      $<$<COMPILE_LANGUAGE:CUDA>:-Xcompiler=-stdlib=libc++>)
  endif()
  oidn_install_static_module(OpenImageDenoise_device_cuda)
  target_link_libraries(OpenImageDenoise PRIVATE OpenImageDenoise_device_cuda)
else()
  oidn_strip_symbols(OpenImageDenoise_device_cuda)
  oidn_install_module(OpenImageDenoise_device_cuda)
endif()''')],
    "cmake/Config.cmake.in": [(
        'include("${CMAKE_CURRENT_LIST_DIR}/OpenImageDenoiseTargets.cmake")',
        '''if(OIDN_STATIC_LIB)
  include(CMakeFindDependencyMacro)
  find_dependency(Threads)
endif()
if(@OIDN_STATIC_CUDA@)
  include(CMakeFindDependencyMacro)
  find_dependency(CUDAToolkit 12.8)
endif()

include("${CMAKE_CURRENT_LIST_DIR}/OpenImageDenoiseTargets.cmake")''')],
}


def patch_source(builder, src_dir: Path) -> None:
    if builder.dry_run:
        return
    pending: list[tuple[Path, bytes]] = []
    for relative, edits in _EDITS.items():
        path = src_dir / relative
        original = path.read_bytes()
        newline = "\r\n" if b"\r\n" in original else "\n"
        text = original.decode("utf-8").replace("\r\n", "\n")
        for old, new in edits:
            if new in text:
                continue
            if text.count(old) != 1:
                raise RuntimeError(f"OIDN v2.5.1 static CUDA patch no longer matches {path}")
            text = text.replace(old, new, 1)
        updated = text.replace("\n", newline).encode("utf-8")
        if updated != original:
            pending.append((path, updated))
    for path, updated in pending:
        path.write_bytes(updated)


def pre_build(builder, repo, ctx, env: dict[str, str]) -> None:
    # Only prepare absent submodules; preserve populated user checkouts.
    # Parse the actual argument order: defaults, repo args, user args/cache.
    # Merging TOML tables alone loses precedence between args and cache entries.
    args = [*builder._repo_cmake_defaults_args("oidn"), *repo.cmake_args,
            *builder._repo_cmake_user_override_args("oidn")]
    cache: dict[str, str] = {}
    for arg in args:
        if arg.startswith("-D") and "=" in arg:
            key, value = arg[2:].split("=", 1)
            cache[key.split(":", 1)[0]] = value

    def is_on(name: str) -> bool:
        return str(cache.get(name, False)).strip().lower() not in {"false", "off", "no", "0", ""}

    required = []
    if is_on("OIDN_FILTER_RT") or is_on("OIDN_FILTER_RTLIGHTMAP"):
        sentinel = "rt_hdr.tza" if is_on("OIDN_FILTER_RT") else "rtlightmap_hdr.tza"
        required.append(("weights", sentinel))
    if is_on("OIDN_DEVICE_CUDA"):
        required.append(("external/cutlass", "include/cutlass/cutlass.h"))
    for relative, sentinel in required:
        directory = ctx.src_dir / relative
        if relative == "weights" and not (directory / sentinel).is_file():
            try:
                run(["git", "lfs", "version"], env=env, dry_run=builder.dry_run)
            except CalledProcessError as error:
                raise RuntimeError(
                    "OIDN weights require Git LFS. Install git-lfs, run git lfs install, "
                    "then retry the build."
                ) from error
        if not directory.is_dir() or not any(p.name != ".git" for p in directory.iterdir()):
            run(["git", "-C", str(ctx.src_dir), "submodule", "update", "--init", "--", relative],
                env=env, dry_run=builder.dry_run)
        if builder.dry_run:
            continue
        if not (directory / sentinel).is_file():
            raise RuntimeError(f"OIDN: incomplete {relative} checkout at {directory}")
        if relative == "weights":
            pointers = []
            for path in directory.glob("*.tza"):
                with path.open("rb") as source:
                    if source.read(80).startswith(b"version https://git-lfs.github.com/spec/v1"):
                        pointers.append(path.name)
            if pointers:
                raise RuntimeError(
                    f"OIDN: Git LFS weight pointers found in {directory}; install Git LFS and run "
                    f"git -C \"{directory}\" lfs pull before building")
