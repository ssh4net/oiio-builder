from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from ..runner import banner, print_cmd, run


STAMP_REVISION = "4"

_FEATURE_DEFINES = (
    "-DSQLITE_ENABLE_FTS5=1",
    "-DSQLITE_ENABLE_RTREE=1",
    "-DSQLITE_ENABLE_GEOPOLY=1",
    "-DSQLITE_ENABLE_MATH_FUNCTIONS=1",
)


def enabled(_builder, _repo) -> bool:
    # SQLite is a useful deliverable in its own right.  CPython also depends on
    # it, but disabling CPython must not make `--only sqlite` unavailable.
    return True


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os in {"windows", "linux", "macos"}:
        return "sqlite"
    return None


def build(builder, ctx, env: dict[str, str]) -> None:
    if builder.platform.os == "windows":
        _build_windows(builder, ctx, env)
        return
    if builder.platform.os in {"linux", "macos"}:
        _build_posix(builder, ctx, env)
        return
    raise RuntimeError(f"Unsupported platform for sqlite build: {builder.platform.os}")


def post_install(builder, install_prefix: Path, build_type: str) -> None:
    if builder.platform.os == "windows":
        return
    _build_posix_zipfile(builder, install_prefix, build_type)
    _write_package_files(builder, install_prefix, build_type)


def _windows_names(builder, build_type: str) -> dict[str, str]:
    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    suffix = debug_postfix if build_type == "Debug" else ""
    return {
        "core_lib": f"sqlite3{suffix}.lib",
        "core_dll": f"sqlite3{suffix}.dll",
        "shell": f"sqlite3{suffix}.exe",
        "zip_lib": f"sqlite3_zipfile{suffix}.lib",
        "zip_dll": f"zipfile{suffix}.dll",
    }


def _windows_zlib_library(builder, ctx) -> Path:
    static_linkage = bool(builder.config.global_cfg.static_default)
    debug = ctx.build_type == "Debug"
    if static_linkage:
        names = ["zlibstaticd.lib", "zlibd.lib"] if debug else ["zlibstatic.lib", "zlib.lib"]
    else:
        names = ["zlibd.lib", "zlib.lib"] if debug else ["zlib.lib"]

    lib_dir = ctx.install_prefix / "lib"
    candidates = [lib_dir / name for name in names]
    selected = next((candidate for candidate in candidates if candidate.exists()), None)
    if selected is not None:
        return selected
    if builder.dry_run:
        return candidates[0]
    wanted = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise RuntimeError(f"sqlite requires the matching zlib-ng library; expected one of:\n{wanted}")


def _windows_tool(env: dict[str, str], names: tuple[str, ...], *, dry_run: bool) -> str:
    path = env.get("PATH") or os.environ.get("PATH", "")
    for name in names:
        resolved = shutil.which(name, path=path)
        if resolved:
            return resolved
    if dry_run:
        return names[0]
    raise RuntimeError(
        f"sqlite requires {names[0]} from a Visual Studio Native Tools environment; "
        "run preflight and verify the Visual Studio C++ workload is installed"
    )


def _nmake_path_value(value: Path | str) -> str:
    text = str(value)
    return f'"{text}"' if any(character.isspace() for character in text) else text


def _windows_nmake_command(builder, ctx, env: dict[str, str]) -> list[str]:
    nmake = _windows_tool(env, ("nmake.exe", "nmake"), dry_run=builder.dry_run)
    compiler = str(builder.toolchain.get("cc") or "cl.exe")
    compiler_name = Path(compiler.strip('"')).name.lower()
    if compiler_name not in {"cl", "cl.exe", "clang-cl", "clang-cl.exe"}:
        raise RuntimeError(
            f"sqlite Makefile.msc requires cl.exe or clang-cl.exe, but the active compiler is {compiler!r}"
        )

    names = _windows_names(builder, ctx.build_type)
    zlib_library = _windows_zlib_library(builder, ctx)
    static_linkage = bool(builder.config.global_cfg.static_default)
    runtime_dynamic = builder._windows_runtime_mode() == "dynamic"

    targets = ["libsqlite3.lib" if static_linkage else names["core_dll"], names["shell"]]
    args = [
        nmake,
        "/nologo",
        "/f",
        str(ctx.src_dir / "Makefile.msc"),
        f"TOP={_nmake_path_value(ctx.src_dir)}",
        f"CC={_nmake_path_value(compiler)}",
        f"NCC={_nmake_path_value(compiler)}",
        "NO_TCL=1",
        "USE_AMALGAMATION=1",
        "MINIMAL_AMALGAMATION=0",
        "USE_ZLIB=1",
        "BUILD_ZLIB=0",
        f"ZLIBINCDIR={_nmake_path_value(ctx.install_prefix / 'include')}",
        f"ZLIBLIBDIR={_nmake_path_value(ctx.install_prefix / 'lib')}",
        f"ZLIBLIB={_nmake_path_value(zlib_library.name)}",
        f"USE_CRT_DLL={1 if runtime_dynamic else 0}",
        # Makefile.msc always suppresses msvcrt.lib, which contradicts its own
        # /MD selection and breaks Release host tools such as lemon.exe.  Keep
        # the makefile default for /MT builds; /MD builds must let MSVC select
        # the matching DLL CRT from the active Native Tools environment.
        *(["LDFLAGS=/DEBUG"] if runtime_dynamic else []),
        f"DYNAMIC_SHELL={0 if static_linkage else 1}",
        f"DEBUG={2 if ctx.build_type == 'Debug' else 0}",
        f"ASAN={1 if ctx.build_type == 'ASAN' else 0}",
        f"SQLITE3DLL={names['core_dll']}",
        f"SQLITE3LIB={names['core_lib']}",
        f"SQLITE3EXE={names['shell']}",
        f"OPTS={' '.join(_FEATURE_DEFINES)}",
        *targets,
    ]
    return args


def _windows_jimsh_command(builder, ctx) -> list[str]:
    compiler = str(builder.toolchain.get("cc") or "cl.exe")
    return [
        compiler,
        "/nologo",
        "/DHAVE__FULLPATH=1",
        str(ctx.src_dir / "autosetup" / "jimsh0.c"),
        f"/Fe{ctx.build_dir / 'jimsh0.exe'}",
    ]


def _clean_build_dir(builder, build_dir: Path) -> None:
    if not builder.dry_run and build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)


def _posix_line_ending_files(src_dir: Path) -> list[Path]:
    paths = [
        src_dir / "Makefile.linux-generic",
        src_dir / "main.mk",
        src_dir / "src" / "parse.y",
        src_dir / "ext" / "fts5" / "fts5parse.y",
    ]
    for root in (src_dir / "tool", src_dir / "ext" / "fts5" / "tool"):
        if not root.is_dir():
            continue
        paths.extend(
            sorted(
                path
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in {".sh", ".tcl"}
            )
        )
    return paths


def _normalize_posix_source_line_endings(builder, src_dir: Path) -> None:
    generator = src_dir / "tool" / "mksqlite3c.tcl"
    if not builder.dry_run and not generator.exists():
        raise RuntimeError(f"Missing SQLite amalgamation generator: {generator}")
    builder._normalize_posix_shell_scripts("sqlite", _posix_line_ending_files(src_dir))


def _posix_make_arguments(builder, ctx) -> list[str]:
    cflags, _, _ = builder._non_cmake_flags(ctx.build_type)
    include_dir = ctx.install_prefix / "include"
    lib_dir = ctx.install_prefix / "lib"
    static_linkage = bool(builder.config.global_cfg.static_default)
    args = [
        f"TOP={ctx.src_dir}",
        f"prefix={ctx.install_prefix}",
        f"CC={builder.toolchain.get('cc') or 'cc'}",
        f"AR={builder.toolchain.get('ar') or 'ar'}",
        "AR.flags=rcs",
        f"CFLAGS={cflags} -I{include_dir}",
        "CFLAGS.core=-fPIC",
        f"OPT_FEATURE_FLAGS={' '.join(_FEATURE_DEFINES)}",
        "SHELL_OPT=-DHAVE_READLINE=0 -DSQLITE_HAVE_ZLIB=1",
        "CFLAGS.readline=",
        "LDFLAGS.readline=",
        f"LDFLAGS.zlib=-L{lib_dir} -lz",
        "HAVE_TCL=0",
        "USE_AMALGAMATION=1",
        f"ENABLE_LIB_STATIC={1 if static_linkage else 0}",
        f"ENABLE_LIB_SHARED={0 if static_linkage else 1}",
    ]
    if builder.platform.os == "macos":
        args.extend(
            [
                "B.dll=.dylib",
                "T.dll=.dylib",
                "LDFLAGS.shlib=-dynamiclib",
                "LDFLAGS.dlopen=",
                "libsqlite3.DLL.install-rules=darwin",
                "LDFLAGS.libsqlite3.os-specific=-Wl,-install_name,@rpath/libsqlite3.dylib",
            ]
        )
    return args


def _build_posix(builder, ctx, env: dict[str, str]) -> None:
    makefile = ctx.src_dir / "Makefile.linux-generic"
    if not builder.dry_run and not makefile.exists():
        raise RuntimeError(f"Missing SQLite generic makefile: {makefile}")
    _normalize_posix_source_line_endings(builder, ctx.src_dir)

    _clean_build_dir(builder, ctx.build_dir)
    make = shutil.which("gmake") or shutil.which("make") or "make"
    make_args = _posix_make_arguments(builder, ctx)
    static_linkage = bool(builder.config.global_cfg.static_default)
    selected_library = "lib" if static_linkage else "so"
    build_cmd = [make, "-f", str(makefile), f"-j{builder._jobs()}", *make_args, selected_library, "sqlite3"]
    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )

    install_cmd = [make, "-f", str(makefile), *make_args, "install"]
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )


def _build_windows(builder, ctx, env: dict[str, str]) -> None:
    makefile = ctx.src_dir / "Makefile.msc"
    if not builder.dry_run and not makefile.exists():
        raise RuntimeError(f"Missing SQLite Windows makefile: {makefile}")

    _clean_build_dir(builder, ctx.build_dir)
    build_env = dict(env)
    for variable in ("CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
        build_env[variable] = ""

    build_cmd = _windows_nmake_command(builder, ctx, build_env)
    bootstrap_cmd = _windows_jimsh_command(builder, ctx)
    print_cmd("bootstrap command", bootstrap_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - bootstrap")
    run(
        bootstrap_cmd,
        cwd=str(ctx.build_dir),
        env=build_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "bootstrap")),
    )

    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(ctx.build_dir),
        env=build_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )

    _build_windows_zipfile(builder, ctx, build_env)
    if builder.dry_run:
        print(f"[dry-run] sqlite: install native artifacts into {ctx.install_prefix}", flush=True)
        return
    _install_windows(builder, ctx)


def _windows_compile_flags(builder, build_type: str) -> list[str]:
    cflags, _, _ = builder._non_cmake_flags(build_type)
    return shlex.split(cflags, posix=False)


def _build_windows_zipfile(builder, ctx, env: dict[str, str]) -> None:
    source = ctx.src_dir / "ext" / "misc" / "zipfile.c"
    if not builder.dry_run and not source.exists():
        raise RuntimeError(f"Missing SQLite zipfile extension source: {source}")

    names = _windows_names(builder, ctx.build_type)
    compiler = str(builder.toolchain.get("cc") or "cl.exe")
    zlib_library = _windows_zlib_library(builder, ctx)
    obj = ctx.build_dir / "sqlite3_zipfile.obj"
    static_linkage = bool(builder.config.global_cfg.static_default)

    compile_cmd = [
        compiler,
        "/nologo",
        *_windows_compile_flags(builder, ctx.build_type),
        f"/I{ctx.build_dir}",
        f"/I{ctx.install_prefix / 'include'}",
        "/DSQLITE_CORE" if static_linkage else "/DSQLITE_ENABLE_LOAD_EXTENSION=1",
        str(source),
        f"/Fo{obj}",
    ]
    if static_linkage:
        compile_cmd.insert(-2, "/c")
    else:
        compile_cmd.extend(
            [
                "/LD",
                f"/Fe{ctx.build_dir / names['zip_dll']}",
                "/link",
                f"/LIBPATH:{ctx.install_prefix / 'lib'}",
                zlib_library.name,
            ]
        )

    print_cmd("zipfile command", compile_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - zipfile")
    run(
        compile_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "zipfile")),
    )

    if static_linkage:
        librarian = _windows_tool(env, ("lib.exe", "lib"), dry_run=builder.dry_run)
        archive_cmd = [librarian, "/nologo", f"/OUT:{ctx.build_dir / names['zip_lib']}", str(obj)]
        print_cmd("zipfile archive command", archive_cmd)
        run(
            archive_cmd,
            cwd=str(ctx.build_dir),
            env=env,
            dry_run=builder.dry_run,
            log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "zipfile-archive")),
        )


def _copy_required(source: Path, destination: Path) -> None:
    if not source.exists():
        raise RuntimeError(f"sqlite build did not produce expected artifact: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _install_windows(builder, ctx) -> None:
    names = _windows_names(builder, ctx.build_type)
    static_linkage = bool(builder.config.global_cfg.static_default)
    include_dir = ctx.install_prefix / "include"
    lib_dir = ctx.install_prefix / "lib"
    bin_dir = ctx.install_prefix / "bin"

    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    _copy_required(ctx.build_dir / "sqlite3.h", include_dir / "sqlite3.h")
    _copy_required(ctx.build_dir / "sqlite3ext.h", include_dir / "sqlite3ext.h")
    _write_zipfile_header(include_dir / "sqlite3_zipfile.h")

    if static_linkage:
        _copy_required(ctx.build_dir / "libsqlite3.lib", lib_dir / names["core_lib"])
        _copy_required(ctx.build_dir / names["zip_lib"], lib_dir / names["zip_lib"])
    else:
        _copy_required(ctx.build_dir / names["core_lib"], lib_dir / names["core_lib"])
        _copy_required(ctx.build_dir / names["core_dll"], bin_dir / names["core_dll"])
        _copy_required(ctx.build_dir / names["zip_dll"], bin_dir / names["zip_dll"])
    _copy_required(ctx.build_dir / names["shell"], bin_dir / names["shell"])

    _write_package_files(builder, ctx.install_prefix, ctx.build_type)


def _build_posix_zipfile(builder, install_prefix: Path, build_type: str) -> None:
    source_root = builder.repo_paths.get("sqlite")
    if source_root is None:
        raise RuntimeError("sqlite source path is unavailable while building its zipfile extension")
    source = source_root / "ext" / "misc" / "zipfile.c"
    if not source.exists():
        raise RuntimeError(f"Missing SQLite zipfile extension source: {source}")

    build_dir = builder.config.global_cfg.build_root / build_type / "sqlite" / "zipfile-extension"
    build_dir.mkdir(parents=True, exist_ok=True)
    compiler = str(builder.toolchain.get("cc") or "cc")
    cflags, _, _ = builder._non_cmake_flags(build_type)
    compile_flags = shlex.split(cflags)
    obj = build_dir / "sqlite3_zipfile.o"
    static_linkage = bool(builder.config.global_cfg.static_default)
    output = (
        install_prefix / "lib" / "libsqlite3_zipfile.a"
        if static_linkage
        else install_prefix / "lib" / ("zipfile.dylib" if builder.platform.os == "macos" else "zipfile.so")
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    compile_cmd = [
        compiler,
        *compile_flags,
        "-fPIC",
        f"-I{install_prefix / 'include'}",
        "-DSQLITE_CORE" if static_linkage else "-DSQLITE_ENABLE_LOAD_EXTENSION=1",
        "-c",
        str(source),
        "-o",
        str(obj),
    ]
    print_cmd("zipfile compile command", compile_cmd)
    banner(f"sqlite ({build_type}) - zipfile")
    run(
        compile_cmd,
        cwd=str(build_dir),
        env=builder._env_for_build(build_type, install_prefix),
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path("sqlite", build_type, "zipfile")),
    )

    if static_linkage:
        archive_cmd = [str(builder.toolchain.get("ar") or "ar"), "rcs", str(output), str(obj)]
        print_cmd("zipfile archive command", archive_cmd)
        run(
            archive_cmd,
            cwd=str(build_dir),
            env=builder._env_for_build(build_type, install_prefix),
            dry_run=builder.dry_run,
            log_path=str(builder._repo_log_path("sqlite", build_type, "zipfile-archive")),
        )
    else:
        link_mode = "-dynamiclib" if builder.platform.os == "macos" else "-shared"
        rpath = "-Wl,-rpath,@loader_path" if builder.platform.os == "macos" else "-Wl,-rpath,$ORIGIN"
        link_cmd = [
            compiler,
            link_mode,
            str(obj),
            f"-L{install_prefix / 'lib'}",
            "-lz",
            rpath,
            "-o",
            str(output),
        ]
        print_cmd("zipfile link command", link_cmd)
        run(
            link_cmd,
            cwd=str(build_dir),
            env=builder._env_for_build(build_type, install_prefix),
            dry_run=builder.dry_run,
            log_path=str(builder._repo_log_path("sqlite", build_type, "zipfile-link")),
        )

    if not builder.dry_run:
        _write_zipfile_header(install_prefix / "include" / "sqlite3_zipfile.h")


def _sqlite_version(builder) -> str:
    source_root = builder.repo_paths.get("sqlite")
    if source_root is None:
        return "0"
    version_file = source_root / "VERSION"
    if not version_file.exists():
        return "0"
    return version_file.read_text(encoding="utf-8", errors="replace").strip() or "0"


def _write_zipfile_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """#pragma once

#include <sqlite3.h>

#ifdef __cplusplus
extern \"C\" {
#endif

int sqlite3_zipfile_init(
    sqlite3* db,
    char** error_message,
    const sqlite3_api_routines* api);

#ifdef __cplusplus
}
#endif
""",
        encoding="utf-8",
    )


def _write_package_files(builder, install_prefix: Path, build_type: str) -> None:
    if builder.dry_run:
        return
    version = _sqlite_version(builder)
    cmake_dir = install_prefix / "lib" / "cmake" / "SQLite3"
    pkgconfig_dir = install_prefix / "lib" / "pkgconfig"
    cmake_dir.mkdir(parents=True, exist_ok=True)
    pkgconfig_dir.mkdir(parents=True, exist_ok=True)
    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    pkg_library = (
        f"sqlite3{debug_postfix}"
        if builder.platform.os == "windows" and build_type == "Debug"
        else "sqlite3"
    )

    (cmake_dir / "SQLite3Config.cmake").write_text(
        _cmake_package_text(builder, build_type, version),
        encoding="utf-8",
    )
    (cmake_dir / "SQLite3ConfigVersion.cmake").write_text(
        _cmake_version_text(version),
        encoding="utf-8",
    )
    (pkgconfig_dir / "sqlite3.pc").write_text(
        "\n".join(
            [
                f"prefix={install_prefix.as_posix()}",
                "exec_prefix=${prefix}",
                "libdir=${prefix}/lib",
                "includedir=${prefix}/include",
                "",
                "Name: SQLite",
                "Description: SQL database engine",
                f"Version: {version}",
                f"Libs: -L${{libdir}} -l{pkg_library}",
                (
                    "Libs.private: -lz"
                    if builder.platform.os == "windows"
                    else "Libs.private: -lz -lm -ldl -lpthread"
                ),
                "Cflags: -I${includedir}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _cmake_package_text(builder, build_type: str, version: str) -> str:
    static_linkage = bool(builder.config.global_cfg.static_default)
    lines = [
        "# Generated by oiio-builder's SQLite source recipe.",
        "include(CMakeFindDependencyMacro)",
        "find_dependency(ZLIB REQUIRED)",
        "get_filename_component(_sqlite_prefix \"${CMAKE_CURRENT_LIST_DIR}/../../..\" ABSOLUTE)",
        "set(SQLite3_INCLUDE_DIR \"${_sqlite_prefix}/include\")",
        "set(SQLite3_INCLUDE_DIRS \"${SQLite3_INCLUDE_DIR}\")",
        f'set(SQLite3_VERSION "{version}")',
        "set(SQLite3_FOUND TRUE)",
        "",
    ]
    if builder.platform.os != "windows" and static_linkage:
        lines.insert(3, "find_dependency(Threads REQUIRED)")

    if builder.platform.os == "windows":
        debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
        core_kind = "STATIC" if static_linkage else "SHARED"
        lines.extend(
            [
                "if(NOT TARGET SQLite::SQLite3)",
                f"add_library(SQLite::SQLite3 {core_kind} IMPORTED)",
                "set_property(TARGET SQLite::SQLite3 PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG;ASAN\")",
                "set_target_properties(SQLite::SQLite3 PROPERTIES",
                "  INTERFACE_INCLUDE_DIRECTORIES \"${SQLite3_INCLUDE_DIR}\"",
            ]
        )
        if static_linkage:
            lines.extend(
                [
                    "  IMPORTED_LOCATION_RELEASE \"${_sqlite_prefix}/lib/sqlite3.lib\"",
                    f'  IMPORTED_LOCATION_DEBUG "${{_sqlite_prefix}}/lib/sqlite3{debug_postfix}.lib"',
                    "  IMPORTED_LOCATION_ASAN \"${_sqlite_prefix}/lib/sqlite3.lib\"",
                ]
            )
        else:
            current_zip_name = f"zipfile{debug_postfix}.dll" if build_type == "Debug" else "zipfile.dll"
            lines.extend(
                [
                    "  IMPORTED_IMPLIB_RELEASE \"${_sqlite_prefix}/lib/sqlite3.lib\"",
                    f'  IMPORTED_IMPLIB_DEBUG "${{_sqlite_prefix}}/lib/sqlite3{debug_postfix}.lib"',
                    "  IMPORTED_IMPLIB_ASAN \"${_sqlite_prefix}/lib/sqlite3.lib\"",
                    "  IMPORTED_LOCATION_RELEASE \"${_sqlite_prefix}/bin/sqlite3.dll\"",
                    f'  IMPORTED_LOCATION_DEBUG "${{_sqlite_prefix}}/bin/sqlite3{debug_postfix}.dll"',
                    "  IMPORTED_LOCATION_ASAN \"${_sqlite_prefix}/bin/sqlite3.dll\"",
                ]
            )
        lines.extend(
            [
                "  MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                "  MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                ")",
                "endif()",
                "",
            ]
        )

        zip_kind = "STATIC" if static_linkage else "MODULE"
        lines.extend(
            [
                "if(NOT TARGET SQLite::Zipfile)",
                f"add_library(SQLite::Zipfile {zip_kind} IMPORTED)",
                "set_property(TARGET SQLite::Zipfile PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG;ASAN\")",
            ]
        )
        if static_linkage:
            lines.extend(
                [
                    "set_target_properties(SQLite::Zipfile PROPERTIES",
                    "  IMPORTED_LOCATION_RELEASE \"${_sqlite_prefix}/lib/sqlite3_zipfile.lib\"",
                    f'  IMPORTED_LOCATION_DEBUG "${{_sqlite_prefix}}/lib/sqlite3_zipfile{debug_postfix}.lib"',
                    "  IMPORTED_LOCATION_ASAN \"${_sqlite_prefix}/lib/sqlite3_zipfile.lib\"",
                    "  INTERFACE_LINK_LIBRARIES \"SQLite::SQLite3;ZLIB::ZLIB\"",
                    "  MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                    "  MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                    ")",
                    "endif()",
                    "set(SQLite3_ZIPFILE_EXTENSION \"\")",
                ]
            )
        else:
            lines.extend(
                [
                    "set_target_properties(SQLite::Zipfile PROPERTIES",
                    "  IMPORTED_LOCATION_RELEASE \"${_sqlite_prefix}/bin/zipfile.dll\"",
                    f'  IMPORTED_LOCATION_DEBUG "${{_sqlite_prefix}}/bin/zipfile{debug_postfix}.dll"',
                    "  IMPORTED_LOCATION_ASAN \"${_sqlite_prefix}/bin/zipfile.dll\"",
                    "  MAP_IMPORTED_CONFIG_MINSIZEREL Release",
                    "  MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
                    ")",
                    "endif()",
                    f'set(SQLite3_ZIPFILE_EXTENSION "${{_sqlite_prefix}}/bin/{current_zip_name}")',
                    f'set(SQLite3_ZIPFILE_EXTENSION_DEBUG "${{_sqlite_prefix}}/bin/zipfile{debug_postfix}.dll")',
                    "set(SQLite3_ZIPFILE_EXTENSION_RELEASE \"${_sqlite_prefix}/bin/zipfile.dll\")",
                ]
            )
    else:
        core_name = (
            "libsqlite3.a"
            if static_linkage
            else ("libsqlite3.dylib" if builder.platform.os == "macos" else "libsqlite3.so")
        )
        zip_name = (
            "libsqlite3_zipfile.a"
            if static_linkage
            else ("zipfile.dylib" if builder.platform.os == "macos" else "zipfile.so")
        )
        core_kind = "STATIC" if static_linkage else "SHARED"
        zip_kind = "STATIC" if static_linkage else "MODULE"
        lines.extend(
            [
                "if(NOT TARGET SQLite::SQLite3)",
                f"add_library(SQLite::SQLite3 {core_kind} IMPORTED)",
                "set_target_properties(SQLite::SQLite3 PROPERTIES",
                "  INTERFACE_INCLUDE_DIRECTORIES \"${SQLite3_INCLUDE_DIR}\"",
                f'  IMPORTED_LOCATION "${{_sqlite_prefix}}/lib/{core_name}"',
            ]
        )
        if static_linkage:
            lines.append('  INTERFACE_LINK_LIBRARIES "Threads::Threads;${CMAKE_DL_LIBS};m"')
        lines.extend(
            [
                ")",
                "endif()",
                "",
                "if(NOT TARGET SQLite::Zipfile)",
                f"add_library(SQLite::Zipfile {zip_kind} IMPORTED)",
                "set_target_properties(SQLite::Zipfile PROPERTIES",
                f'  IMPORTED_LOCATION "${{_sqlite_prefix}}/lib/{zip_name}"',
            ]
        )
        if static_linkage:
            lines.append("  INTERFACE_LINK_LIBRARIES \"SQLite::SQLite3;ZLIB::ZLIB\"")
        lines.extend(
            [
                ")",
                "endif()",
                (
                    "set(SQLite3_ZIPFILE_EXTENSION \"\")"
                    if static_linkage
                    else f'set(SQLite3_ZIPFILE_EXTENSION "${{_sqlite_prefix}}/lib/{zip_name}")'
                ),
            ]
        )

    lines.extend(
        [
            "",
            "if(NOT TARGET SQLite3::SQLite3)",
            "  add_library(SQLite3::SQLite3 INTERFACE IMPORTED)",
            "  set_property(TARGET SQLite3::SQLite3 PROPERTY INTERFACE_LINK_LIBRARIES SQLite::SQLite3)",
            "endif()",
            "set(SQLite3_LIBRARY SQLite::SQLite3)",
            "set(SQLite3_LIBRARIES SQLite::SQLite3)",
            "unset(_sqlite_prefix)",
            "",
        ]
    )
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
