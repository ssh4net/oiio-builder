# Builder Design Requirements

This document captures global and per-platform requirements for the cross-platform build system (Python-based orchestrator, uv/venv). It is intended as a living checklist and design contract.

## Global Requirements
- Tooling: Python 3 + astral-uv + venv for a single cross-platform entrypoint.
- Build system: CMake + Ninja preferred (MSBuild allowed on Windows).
- Language standard: default C++20, allow per-project overrides (some deps may require C++17).
- Build types: Debug, Release, ASAN; allow selecting a subset (e.g., Debug-only).
- Static builds by default; allow dynamic linking for system/NVIDIA-provided components.
- CXX extensions: OFF by default (GNU/Clang extensions disabled).
- libc++ preferred on macOS/Linux; provide an explicit switch to build with libstdc++.
- Repo management: clone/update by branch/tag/commit; support fast update and pinning.
- Rebuild policy: stamps based on repo HEAD + toolchain + flags + dependency SHAs.
- Install layout: optional user-defined base prefix on Unix that expands to per-config prefixes; single prefix on Windows with debug postfix.
- Logging: concise, grep-friendly, with per-repo build logs.

## Platform Requirements

### macOS (arm64/x64)
- Toolchain: Apple Clang (Xcode), C++20 via libc++.
- Build flags:
  - `-stdlib=libc++`
  - `-fno-rtti` only when required by a dependency (avoid global default).
  - `-fPIC` for static libs used in shared contexts.
  - `-DCMAKE_CXX_EXTENSIONS=OFF`
- Linker: default `ld64`; avoid Linux-specific flags.
- OpenMP: via Homebrew `libomp` (CMake `OpenMP_ROOT` or `CMAKE_PREFIX_PATH`).
- Debug/Release/ASAN:
  - ASAN: `-fsanitize=address -fno-omit-frame-pointer`.
- Install layout: if `PREFIX_BASE=/mnt/f/UBS`, use `UBS` (Release), `UBSd` (Debug), `UBSa` (ASAN); allow explicit overrides.

### Linux (x64/arm64)
- Toolchain: Clang + libc++ preferred; allow libstdc++ via option.
- Build flags:
  - `-stdlib=libc++` when using libc++.
  - `-DCMAKE_CXX_EXTENSIONS=OFF`
  - `-fPIC` for static libs.
- OpenMP: prefer system `libomp` or LLVM OpenMP package.
- ASAN: `-fsanitize=address -fno-omit-frame-pointer`.
- Install layout: if `PREFIX_BASE=/mnt/f/UBS`, use `UBS` (Release), `UBSd` (Debug), `UBSa` (ASAN); allow explicit overrides.

### Windows (x64/arm64)
- Generators (selectable per run):
  - `MSVC + MSVC` (Visual Studio solution)
  - `Ninja + MSVC`
  - `MSVC + clang-cl` (Visual Studio solution, `-T ClangCL`)
  - `Ninja + clang-cl`
- Runtime: consistent `/MD` or `/MDd` across all libs and consumers.
- Debug postfix: use `d` and install Debug/Release into one prefix (Debug builds first).
- ASAN:
  - Supported with MSVC `/fsanitize=address` (x64/x86; ARM64 in preview).
  - Prefer clang-cl for consistent ASAN behavior.
- Install layout: single prefix for Debug + Release; use `CMAKE_DEBUG_POSTFIX=d`. ASAN can use a separate prefix with `_ASAN` suffix (e.g., `E:\\DVS_ASAN`).

## Documentation Tools
- Sphinx: optional documentation build target.
- Doxygen: optional API documentation target.
- Build system should allow toggling these on/off per repo or globally.

## Open Questions / TBD
- Exact prefix naming convention for ASAN on Unix.
- Whether to force Ninja globally or allow MSBuild by default on Windows.
- Centralized policy for LTO and RTTI (per repo vs global).
