from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from ..runner import banner, print_cmd, run


STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_libsodium)


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os == "windows":
        return "libsodium"
    if builder.platform.os in {"linux", "macos"}:
        return "autotools"
    return None


def build(builder, ctx, env: dict[str, str]) -> None:
    if builder.platform.os != "windows":
        raise RuntimeError("The custom libsodium backend is only used for native Windows builds")
    _build_windows(builder, ctx, env)


def install_only(builder, ctx, _env: dict[str, str]) -> bool:
    if builder.platform.os != "windows":
        return False
    output_dir = _windows_output_dir(ctx)
    target_name = _windows_target_name(builder, ctx.build_type)
    required = [output_dir / f"{target_name}.lib"]
    if not builder.config.global_cfg.static_default:
        required.append(output_dir / f"{target_name}.dll")
    if not all(path.exists() for path in required):
        return False
    _install_windows(builder, ctx)
    return True


def post_install(builder, install_prefix: Path, build_type: str) -> None:
    _write_package_files(builder, install_prefix, build_type)


def _windows_platform(builder) -> str:
    platforms = {
        "x86_64": "x64",
        "arm64": "ARM64",
        "x86": "Win32",
        "i686": "Win32",
    }
    platform = platforms.get(builder.platform.arch)
    if platform is None:
        raise RuntimeError(f"Unsupported Windows architecture for libsodium: {builder.platform.arch}")
    return platform


def _windows_configuration(builder, build_type: str) -> str:
    if build_type == "ASAN":
        raise RuntimeError(
            "libsodium's upstream VS2022 solution has no ASAN configuration; "
            "exclude libsodium from native Windows ASAN builds"
        )
    if build_type not in {"Debug", "Release"}:
        raise RuntimeError(f"Unsupported Windows build type for libsodium: {build_type}")
    linkage = "Static" if builder.config.global_cfg.static_default else "Dyn"
    return f"{linkage}{build_type}"


def _windows_target_name(builder, build_type: str) -> str:
    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    return f"libsodium{debug_postfix}" if build_type == "Debug" else "libsodium"


def _validate_windows_toolchain(builder) -> None:
    compiler = Path(str(builder.toolchain.get("cc") or "cl.exe").strip('"')).name.lower()
    if compiler not in {"cl", "cl.exe"}:
        raise RuntimeError(
            "The libsodium Windows recipe uses the upstream VS2022 MSVC project; "
            "select the msvc or ninja-msvc generator."
        )


def _windows_runtime_library(builder, build_type: str) -> str:
    runtime_mode = builder._windows_runtime_mode()
    runtime_libraries = {
        ("static", "Debug"): "MultiThreadedDebug",
        ("static", "Release"): "MultiThreaded",
        ("dynamic", "Debug"): "MultiThreadedDebugDLL",
        ("dynamic", "Release"): "MultiThreadedDLL",
    }
    runtime_library = runtime_libraries.get((runtime_mode, build_type))
    if runtime_library is None:
        raise RuntimeError(
            f"Unsupported libsodium Windows runtime/build type: {runtime_mode}/{build_type}"
        )
    return runtime_library


def _windows_runtime_props_path(ctx) -> Path:
    return ctx.build_dir / "oiio-builder-msvc-runtime.props"


def _windows_runtime_props_text(builder, build_type: str) -> str:
    runtime_library = _windows_runtime_library(builder, build_type)
    undefine_dll = ""
    if builder._windows_runtime_mode() == "static":
        undefine_dll = (
            "\n      <UndefinePreprocessorDefinitions>"
            "_DLL;%(UndefinePreprocessorDefinitions)"
            "</UndefinePreprocessorDefinitions>"
        )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">
  <ItemDefinitionGroup>
    <ClCompile>
      <RuntimeLibrary>{runtime_library}</RuntimeLibrary>{undefine_dll}
    </ClCompile>
  </ItemDefinitionGroup>
</Project>
"""


def _write_windows_runtime_props(builder, ctx) -> Path:
    props_path = _windows_runtime_props_path(ctx)
    if not builder.dry_run:
        props_path.write_text(
            _windows_runtime_props_text(builder, ctx.build_type),
            encoding="utf-8",
        )
    return props_path


def _msbuild_executable(builder, env: dict[str, str]) -> str:
    search_path = env.get("PATH") or os.environ.get("PATH", "")
    for name in ("msbuild.exe", "msbuild"):
        executable = shutil.which(name, path=search_path)
        if executable:
            return executable
    if builder.dry_run:
        return "msbuild.exe"
    raise RuntimeError(
        "libsodium requires msbuild.exe from a Visual Studio 2022 Native Tools environment"
    )


def _windows_solution(ctx) -> Path:
    return ctx.src_dir / "builds" / "msvc" / "vs2022" / "libsodium.sln"


def _windows_output_dir(ctx) -> Path:
    return ctx.build_dir / "out"


def _windows_build_command(builder, ctx, env: dict[str, str]) -> list[str]:
    _validate_windows_toolchain(builder)
    solution = _windows_solution(ctx)
    if not builder.dry_run and not solution.exists():
        raise RuntimeError(f"Missing libsodium VS2022 solution: {solution}")

    output_dir = _windows_output_dir(ctx)
    object_dir = ctx.build_dir / "obj"
    target_name = _windows_target_name(builder, ctx.build_type)
    runtime_props = _windows_runtime_props_path(ctx)
    return [
        _msbuild_executable(builder, env),
        str(solution),
        "/nologo",
        "/verbosity:minimal",
        f"/maxCpuCount:{builder._jobs()}",
        "/target:Build",
        f"/property:Configuration={_windows_configuration(builder, ctx.build_type)}",
        f"/property:Platform={_windows_platform(builder)}",
        f"/property:OutDir={output_dir}{os.sep}",
        f"/property:IntDir={object_dir}{os.sep}",
        f"/property:TargetName={target_name}",
        f"/property:ForceImportBeforeCppTargets={runtime_props}",
    ]


def _clean_build_dir(builder, build_dir: Path) -> None:
    if not builder.dry_run and build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)


def _build_windows(builder, ctx, env: dict[str, str]) -> None:
    _clean_build_dir(builder, ctx.build_dir)
    _write_windows_runtime_props(builder, ctx)
    command = _windows_build_command(builder, ctx, env)
    print_cmd("build command", command)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        command,
        cwd=str(ctx.src_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )
    if builder.dry_run:
        print(f"[dry-run] libsodium: install native artifacts into {ctx.install_prefix}", flush=True)
        return
    _install_windows(builder, ctx)


def _copy_required(source: Path, destination: Path) -> None:
    if not source.exists():
        raise RuntimeError(f"libsodium build did not produce expected artifact: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _copy_optional(source: Path, destination: Path) -> None:
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _install_windows_headers(ctx) -> None:
    source_include = ctx.src_dir / "src" / "libsodium" / "include"
    sodium_source = source_include / "sodium"
    if not (source_include / "sodium.h").exists() or not sodium_source.is_dir():
        raise RuntimeError(f"Missing libsodium public headers below: {source_include}")

    include_dir = ctx.install_prefix / "include"
    _copy_required(source_include / "sodium.h", include_dir / "sodium.h")
    public_headers = sorted(sodium_source.glob("*.h"))
    for source in public_headers:
        _copy_required(source, include_dir / "sodium" / source.name)

    version_header = sodium_source / "version.h"
    if not version_header.exists():
        version_header = ctx.src_dir / "builds" / "msvc" / "version.h"
    _copy_required(version_header, include_dir / "sodium" / "version.h")


def _install_windows(builder, ctx) -> None:
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    _install_windows_headers(ctx)

    target_name = _windows_target_name(builder, ctx.build_type)
    output_dir = _windows_output_dir(ctx)
    lib_dir = ctx.install_prefix / "lib"
    bin_dir = ctx.install_prefix / "bin"
    _copy_required(output_dir / f"{target_name}.lib", lib_dir / f"{target_name}.lib")
    if not builder.config.global_cfg.static_default:
        _copy_required(output_dir / f"{target_name}.dll", bin_dir / f"{target_name}.dll")
        _copy_optional(output_dir / f"{target_name}.pdb", bin_dir / f"{target_name}.pdb")


def _sodium_version(builder, install_prefix: Path) -> str:
    candidates = [install_prefix / "include" / "sodium" / "version.h"]
    source_root = builder.repo_paths.get("libsodium")
    if source_root is not None:
        candidates.extend(
            [
                source_root / "src" / "libsodium" / "include" / "sodium" / "version.h",
                source_root / "builds" / "msvc" / "version.h",
            ]
        )
    for path in candidates:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        match = re.search(r'^#define\s+SODIUM_VERSION_STRING\s+"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    return "0"


def _write_package_files(builder, install_prefix: Path, build_type: str) -> None:
    if builder.dry_run:
        return
    version = _sodium_version(builder, install_prefix)
    cmake_dir = install_prefix / "lib" / "cmake" / "sodium"
    cmake_dir.mkdir(parents=True, exist_ok=True)
    (cmake_dir / "sodiumConfig.cmake").write_text(
        _cmake_package_text(builder, version),
        encoding="utf-8",
    )
    (cmake_dir / "sodiumConfigVersion.cmake").write_text(
        _cmake_version_text(version),
        encoding="utf-8",
    )

    if builder.platform.os == "windows":
        pkgconfig_dir = install_prefix / "lib" / "pkgconfig"
        pkgconfig_dir.mkdir(parents=True, exist_ok=True)
        library_name = _windows_target_name(builder, build_type)
        static_define = " -DSODIUM_STATIC" if builder.config.global_cfg.static_default else ""
        (pkgconfig_dir / "libsodium.pc").write_text(
            "\n".join(
                [
                    f"prefix={install_prefix.as_posix()}",
                    "exec_prefix=${prefix}",
                    "libdir=${prefix}/lib",
                    "includedir=${prefix}/include",
                    "",
                    "Name: libsodium",
                    "Description: A modern and easy-to-use crypto library",
                    f"Version: {version}",
                    f"Libs: -L${{libdir}} -l{library_name}",
                    "Libs.private: -ladvapi32",
                    f"Cflags: -I${{includedir}}{static_define}",
                    "",
                ]
            ),
            encoding="utf-8",
        )


def _cmake_package_text(builder, version: str) -> str:
    static_linkage = bool(builder.config.global_cfg.static_default)
    library_kind = "STATIC" if static_linkage else "SHARED"
    lines = [
        "# Generated by oiio-builder's libsodium recipe.",
        "include(CMakeFindDependencyMacro)",
        "get_filename_component(_sodium_prefix \"${CMAKE_CURRENT_LIST_DIR}/../../..\" ABSOLUTE)",
        "set(sodium_INCLUDE_DIR \"${_sodium_prefix}/include\")",
        "set(sodium_INCLUDE_DIRS \"${sodium_INCLUDE_DIR}\")",
        f'set(sodium_VERSION "{version}")',
        "set(sodium_FOUND TRUE)",
        "set(SODIUM_FOUND TRUE)",
        "",
    ]
    if builder.platform.os != "windows" and static_linkage:
        lines.insert(2, "find_dependency(Threads REQUIRED)")

    if builder.platform.os == "windows":
        debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
        lines.extend(
            [
                "if(NOT TARGET sodium::sodium)",
                f"  add_library(sodium::sodium {library_kind} IMPORTED)",
                "  set_property(TARGET sodium::sodium PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG\")",
                "  set_target_properties(sodium::sodium PROPERTIES",
                "    INTERFACE_INCLUDE_DIRECTORIES \"${sodium_INCLUDE_DIR}\"",
            ]
        )
        if static_linkage:
            lines.extend(
                [
                    "    IMPORTED_LOCATION_RELEASE \"${_sodium_prefix}/lib/libsodium.lib\"",
                    f'    IMPORTED_LOCATION_DEBUG "${{_sodium_prefix}}/lib/libsodium{debug_postfix}.lib"',
                    "    INTERFACE_COMPILE_DEFINITIONS \"SODIUM_STATIC\"",
                    "    INTERFACE_LINK_LIBRARIES \"advapi32\"",
                ]
            )
        else:
            lines.extend(
                [
                    "    IMPORTED_IMPLIB_RELEASE \"${_sodium_prefix}/lib/libsodium.lib\"",
                    f'    IMPORTED_IMPLIB_DEBUG "${{_sodium_prefix}}/lib/libsodium{debug_postfix}.lib"',
                    "    IMPORTED_LOCATION_RELEASE \"${_sodium_prefix}/bin/libsodium.dll\"",
                    f'    IMPORTED_LOCATION_DEBUG "${{_sodium_prefix}}/bin/libsodium{debug_postfix}.dll"',
                ]
            )
        lines.extend(
            [
                "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                "    MAP_IMPORTED_CONFIG_ASAN Release",
                "  )",
                "endif()",
            ]
        )
    else:
        library_name = "libsodium.a" if static_linkage else (
            "libsodium.dylib" if builder.platform.os == "macos" else "libsodium.so"
        )
        lines.extend(
            [
                "if(NOT TARGET sodium::sodium)",
                f"  add_library(sodium::sodium {library_kind} IMPORTED)",
                "  set_target_properties(sodium::sodium PROPERTIES",
                "    INTERFACE_INCLUDE_DIRECTORIES \"${sodium_INCLUDE_DIR}\"",
                f'    IMPORTED_LOCATION "${{_sodium_prefix}}/lib/{library_name}"',
            ]
        )
        if static_linkage:
            lines.append('    INTERFACE_LINK_LIBRARIES "Threads::Threads"')
        lines.extend(["  )", "endif()"])
    lines.extend(["set(sodium_LIBRARIES sodium::sodium)", ""])
    return "\n".join(lines)


def _cmake_version_text(version: str) -> str:
    return f"""set(PACKAGE_VERSION \"{version}\")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
"""
