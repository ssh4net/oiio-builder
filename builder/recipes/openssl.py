from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from ..runner import banner, print_cmd, run
from ..tooling import normalize_override, resolve_executable_candidate, resolve_nasm_executable


STAMP_REVISION = "3"

_WINDOWS_CONFIGURE_FLAG_ENV = (
    "CL",
    "_CL_",
    "CFLAGS",
    "CXXFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
)


def enabled(_builder, _repo) -> bool:
    # OpenSSL is a useful deliverable independently of Qt.  It is permissively
    # licensed and is therefore available in both managed license profiles.
    return True


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os in {"windows", "linux", "macos"}:
        return "openssl"
    return None


def _clean_build_dir(builder, build_dir: Path) -> None:
    if not builder.dry_run and build_dir.exists():
        def _remove_readonly(func, failed_path, exc_info) -> None:
            error = exc_info[1]
            if not isinstance(error, PermissionError):
                raise error
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
            func(failed_path)

        # OpenSSL marks generated scripts such as apps/CA.pl read-only on
        # Windows. A normal retry must clear that attribute while removing the
        # disposable build tree.
        shutil.rmtree(build_dir, onerror=_remove_readonly)
    build_dir.mkdir(parents=True, exist_ok=True)


def _resolve_tool(
    env: dict[str, str],
    candidates: tuple[str, ...],
    *,
    override_names: tuple[str, ...] = (),
    dry_run: bool,
) -> str:
    search_path = env.get("PATH") or os.environ.get("PATH", "")
    for name in override_names:
        candidate = normalize_override(env.get(name) or os.environ.get(name))
        resolved = resolve_executable_candidate(candidate, search_path=search_path)
        if resolved:
            return resolved
    for candidate in candidates:
        resolved = resolve_executable_candidate(candidate, search_path=search_path)
        if resolved:
            return resolved
    if dry_run:
        return candidates[0]
    names = ", ".join((*override_names, *candidates))
    raise RuntimeError(f"OpenSSL requires an executable from: {names}")


def _perl_executable(builder, env: dict[str, str]) -> str:
    candidates = ("perl.exe", "perl") if builder.platform.os == "windows" else ("perl",)
    return _resolve_tool(
        env,
        candidates,
        override_names=("OPENSSL_PERL", "PERL_EXECUTABLE", "PERL"),
        dry_run=builder.dry_run,
    )


def _make_executable(builder, env: dict[str, str]) -> str:
    candidates = ("nmake.exe", "nmake") if builder.platform.os == "windows" else ("gmake", "make")
    return _resolve_tool(env, candidates, dry_run=builder.dry_run)


def _require_nasm(builder, env: dict[str, str]) -> str | None:
    if builder.platform.arch != "x86_64":
        return None
    effective_env = dict(os.environ)
    effective_env.update(env)
    nasm = resolve_nasm_executable(effective_env, platform_os=builder.platform.os)
    if nasm:
        return nasm
    if builder.dry_run:
        return "nasm.exe" if builder.platform.os == "windows" else "nasm"
    hint = (
        "set NASM_EXECUTABLE in [windows.env]"
        if builder.platform.os == "windows"
        else "install the nasm package"
    )
    raise RuntimeError(f"OpenSSL x86-64 builds require NASM. Put it on PATH or {hint}.")


def _windows_base_target(builder) -> str:
    compiler = Path(str(builder.toolchain.get("cc") or "cl.exe").strip('"')).name.lower()
    if compiler not in {"cl", "cl.exe"}:
        raise RuntimeError(
            "The OpenSSL Windows source recipe currently requires MSVC cl.exe; "
            "select the msvc or ninja-msvc generator."
        )
    if builder.platform.arch == "x86_64":
        return "VC-WIN64A"
    if builder.platform.arch == "arm64":
        return "VC-WIN64-ARM"
    if builder.platform.arch in {"x86", "i686"}:
        return "VC-WIN32"
    raise RuntimeError(f"Unsupported Windows architecture for OpenSSL: {builder.platform.arch}")


def _posix_target(builder) -> str:
    targets = {
        ("linux", "x86_64"): "linux-x86_64",
        ("linux", "arm64"): "linux-aarch64",
        ("macos", "x86_64"): "darwin64-x86_64-cc",
        ("macos", "arm64"): "darwin64-arm64-cc",
    }
    target = targets.get((builder.platform.os, builder.platform.arch))
    if target is None:
        raise RuntimeError(
            f"Unsupported platform for the OpenSSL source recipe: "
            f"{builder.platform.os} {builder.platform.arch}"
        )
    return target


def _windows_install_root(ctx) -> Path:
    # Windows profile prefixes intentionally combine configurations.  Keep the
    # complete Debug runtime below Debug/ so openssl.cnf and provider modules do
    # not collide with Release; compatibility libraries are exposed later.
    return ctx.install_prefix / "Debug" if ctx.build_type == "Debug" else ctx.install_prefix


def _windows_variant_target(builder, ctx) -> tuple[str, Path | None]:
    base_target = _windows_base_target(builder)
    if ctx.build_type != "Debug":
        return base_target, None
    target = f"{base_target}-oiio-debug"
    config_path = ctx.build_dir / "oiio-builder-openssl.conf"
    if not builder.dry_run:
        config_path.write_text(
            "my %targets = (\n"
            f'    "{target}" => {{\n'
            f'        inherit_from => [ "{base_target}" ],\n'
            '        shlib_variant => "d",\n'
            "    },\n"
            ");\n",
            encoding="utf-8",
        )
    return target, config_path


def _configure_command(builder, ctx, env: dict[str, str]) -> list[str]:
    perl = _perl_executable(builder, env)
    static_linkage = bool(builder.config.global_cfg.static_default)
    if builder.platform.os == "windows":
        target, custom_config = _windows_variant_target(builder, ctx)
        install_root = _windows_install_root(ctx)
    else:
        target = _posix_target(builder)
        custom_config = None
        install_root = ctx.install_prefix

    openssldir = install_root / "ssl"
    args = [
        perl,
        str(ctx.src_dir / "Configure"),
        target,
        f"--prefix={install_root}",
        f"--openssldir={openssldir}",
        "--libdir=lib",
        "no-tests",
        "no-docs",
        "no-makedepend",
        "no-shared" if static_linkage else "shared",
        "--debug" if ctx.build_type == "Debug" else "--release",
    ]
    if ctx.build_type == "ASAN":
        args.append("enable-asan")
    if custom_config is not None:
        args.append(f"--config={custom_config}")
    return args


def _build_environment(builder, ctx, env: dict[str, str]) -> dict[str, str]:
    build_env = dict(env)
    build_env["PERL"] = _perl_executable(builder, build_env)
    if builder.platform.os == "windows":
        # These variables replace OpenSSL target defaults even when set to an
        # empty value. In particular, empty LDFLAGS drops MSVC's /debug flag,
        # so OpenSSL installs DLLs and then fails while copying PDBs that were
        # never generated. Keep the recipe environment clean and ask run() to
        # remove inherited copies from the native process environment too.
        for variable in _WINDOWS_CONFIGURE_FLAG_ENV:
            build_env.pop(variable, None)
    else:
        cflags, _, ldflags = builder._non_cmake_flags(ctx.build_type)
        build_env["CC"] = str(builder.toolchain.get("cc") or "cc")
        build_env["AR"] = str(builder.toolchain.get("ar") or "ar")
        build_env["RANLIB"] = str(builder.toolchain.get("ranlib") or "ranlib")
        build_env["CFLAGS"] = cflags
        if ldflags:
            build_env["LDFLAGS"] = ldflags
    return build_env


def _run_source_build(builder, ctx, env: dict[str, str]) -> None:
    configure = ctx.src_dir / "Configure"
    if not builder.dry_run and not configure.exists():
        raise RuntimeError(f"Missing OpenSSL Configure script: {configure}")

    _clean_build_dir(builder, ctx.build_dir)
    build_env = _build_environment(builder, ctx, env)
    _require_nasm(builder, build_env)
    configure_cmd = _configure_command(builder, ctx, build_env)
    make = _make_executable(builder, build_env)

    print_cmd("configure command", configure_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
    run(
        configure_cmd,
        cwd=str(ctx.build_dir),
        env=build_env,
        unset_env=_WINDOWS_CONFIGURE_FLAG_ENV if builder.platform.os == "windows" else None,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
    )

    build_cmd = [make]
    if builder.platform.os == "windows":
        build_cmd.append("/nologo")
    else:
        build_cmd.append(f"-j{builder._jobs()}")
    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(ctx.build_dir),
        env=build_env,
        unset_env=_WINDOWS_CONFIGURE_FLAG_ENV if builder.platform.os == "windows" else None,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )

    install_cmd = [make]
    if builder.platform.os == "windows":
        install_cmd.append("/nologo")
    install_cmd.extend(["install_sw", "install_ssldirs"])
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(ctx.build_dir),
        env=build_env,
        unset_env=_WINDOWS_CONFIGURE_FLAG_ENV if builder.platform.os == "windows" else None,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )


def _openssl_version(source_root: Path) -> str:
    values: dict[str, str] = {}
    version_file = source_root / "VERSION.dat"
    if version_file.exists():
        for raw_line in version_file.read_text(encoding="utf-8", errors="replace").splitlines():
            key, separator, value = raw_line.partition("=")
            if separator:
                values[key.strip()] = value.strip()
    return ".".join(values.get(key, "0") for key in ("MAJOR", "MINOR", "PATCH"))


def _windows_expected_dll(builder, source_root: Path, stem: str, *, debug: bool) -> str:
    major = _openssl_version(source_root).split(".", 1)[0]
    architecture = {
        "x86_64": "-x64",
        "arm64": "-arm64",
        "x86": "",
        "i686": "",
    }.get(builder.platform.arch, "")
    return f"{stem}-{major}{architecture}{'d' if debug else ''}.dll"


def _windows_dll_name(builder, install_prefix: Path, source_root: Path, stem: str, *, debug: bool) -> str:
    expected = _windows_expected_dll(builder, source_root, stem, debug=debug)
    bin_dir = install_prefix / "bin"
    if not bin_dir.exists():
        return expected
    candidates = sorted(bin_dir.glob(f"{stem}-*.dll"), key=lambda path: path.name.lower())
    matching = [path for path in candidates if path.stem.lower().endswith("d") == debug]
    return matching[0].name if matching else expected


def _copy_windows_debug_compat(builder, ctx) -> None:
    if builder.dry_run or ctx.build_type != "Debug":
        return
    debug_root = _windows_install_root(ctx)
    final_lib = ctx.install_prefix / "lib"
    final_bin = ctx.install_prefix / "bin"
    final_include = ctx.install_prefix / "include"
    final_lib.mkdir(parents=True, exist_ok=True)
    final_bin.mkdir(parents=True, exist_ok=True)

    if (debug_root / "include").exists():
        shutil.copytree(debug_root / "include", final_include, dirs_exist_ok=True)

    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    for stem in ("libcrypto", "libssl"):
        source = debug_root / "lib" / f"{stem}.lib"
        if source.exists():
            shutil.copy2(source, final_lib / f"{stem}{debug_postfix}.lib")

    for source in (debug_root / "bin").glob("*.dll"):
        shutil.copy2(source, final_bin / source.name)
    for source in (debug_root / "bin").glob("*.pdb"):
        if source.stem.lower().endswith("d"):
            shutil.copy2(source, final_bin / source.name)
    debug_program = debug_root / "bin" / "openssl.exe"
    if debug_program.exists():
        shutil.copy2(debug_program, final_bin / f"openssl{debug_postfix}.exe")
    debug_program_pdb = debug_root / "bin" / "openssl.pdb"
    if debug_program_pdb.exists():
        shutil.copy2(debug_program_pdb, final_bin / f"openssl{debug_postfix}.pdb")


def _windows_cmake_package_text(builder, source_root: Path) -> str:
    version = _openssl_version(source_root)
    static_linkage = bool(builder.config.global_cfg.static_default)
    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    kind = "STATIC" if static_linkage else "SHARED"
    release_crypto_dll = _windows_dll_name(
        builder, builder.prefixes["Release"], source_root, "libcrypto", debug=False
    )
    release_ssl_dll = _windows_dll_name(
        builder, builder.prefixes["Release"], source_root, "libssl", debug=False
    )
    debug_crypto_dll = _windows_dll_name(
        builder, builder.prefixes["Debug"], source_root, "libcrypto", debug=True
    )
    debug_ssl_dll = _windows_dll_name(
        builder, builder.prefixes["Debug"], source_root, "libssl", debug=True
    )

    lines = [
        "# Generated by oiio-builder's OpenSSL source recipe.",
        "get_filename_component(_openssl_prefix \"${CMAKE_CURRENT_LIST_DIR}/../../..\" ABSOLUTE)",
        f'set(OPENSSL_VERSION "{version}")',
        f'set(OpenSSL_VERSION "{version}")',
        "set(OpenSSL_FOUND TRUE)",
        "set(OPENSSL_FOUND TRUE)",
        "set(OPENSSL_ROOT_DIR \"${_openssl_prefix}\")",
        "set(OPENSSL_INCLUDE_DIR \"${_openssl_prefix}/include\")",
        "set(OPENSSL_INCLUDE_DIRS \"${OPENSSL_INCLUDE_DIR}\")",
        "set(OPENSSL_SSL_LIBRARY \"${_openssl_prefix}/lib/libssl.lib\")",
        "set(OPENSSL_CRYPTO_LIBRARY \"${_openssl_prefix}/lib/libcrypto.lib\")",
        "set(OPENSSL_LIBRARIES \"${OPENSSL_SSL_LIBRARY};${OPENSSL_CRYPTO_LIBRARY}\")",
        "set(OPENSSL_RUNTIME_DIR \"${_openssl_prefix}/bin\")",
        "set(OPENSSL_MODULES_DIR \"${_openssl_prefix}/lib/ossl-modules\")",
        "set(OPENSSL_MODULES_DIR_DEBUG \"${_openssl_prefix}/Debug/lib/ossl-modules\")",
        "set(OPENSSL_PROGRAM \"${_openssl_prefix}/bin/openssl.exe\")",
        "set(OPENSSL_PROGRAM_DEBUG \"${_openssl_prefix}/bin/openssl" + debug_postfix + ".exe\")",
        "",
        "if(NOT TARGET OpenSSL::Crypto)",
        f"  add_library(OpenSSL::Crypto {kind} IMPORTED)",
        "  set_property(TARGET OpenSSL::Crypto PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG;ASAN\")",
        "  set_target_properties(OpenSSL::Crypto PROPERTIES",
        "    INTERFACE_INCLUDE_DIRECTORIES \"${OPENSSL_INCLUDE_DIR}\"",
    ]
    if static_linkage:
        lines.extend(
            [
                "    IMPORTED_LOCATION_RELEASE \"${_openssl_prefix}/lib/libcrypto.lib\"",
                f'    IMPORTED_LOCATION_DEBUG "${{_openssl_prefix}}/lib/libcrypto{debug_postfix}.lib"',
                "    IMPORTED_LOCATION_ASAN \"${_openssl_prefix}/lib/libcrypto.lib\"",
            ]
        )
    else:
        lines.extend(
            [
                "    IMPORTED_IMPLIB_RELEASE \"${_openssl_prefix}/lib/libcrypto.lib\"",
                f'    IMPORTED_IMPLIB_DEBUG "${{_openssl_prefix}}/lib/libcrypto{debug_postfix}.lib"',
                "    IMPORTED_IMPLIB_ASAN \"${_openssl_prefix}/lib/libcrypto.lib\"",
                f'    IMPORTED_LOCATION_RELEASE "${{_openssl_prefix}}/bin/{release_crypto_dll}"',
                f'    IMPORTED_LOCATION_DEBUG "${{_openssl_prefix}}/bin/{debug_crypto_dll}"',
                f'    IMPORTED_LOCATION_ASAN "${{_openssl_prefix}}/bin/{release_crypto_dll}"',
            ]
        )
    lines.extend(
        [
            "    INTERFACE_LINK_LIBRARIES \"ws2_32;gdi32;advapi32;crypt32;user32\"",
            "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
            "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
            "  )",
            "endif()",
            "",
            "if(NOT TARGET OpenSSL::SSL)",
            f"  add_library(OpenSSL::SSL {kind} IMPORTED)",
            "  set_property(TARGET OpenSSL::SSL PROPERTY IMPORTED_CONFIGURATIONS \"RELEASE;DEBUG;ASAN\")",
            "  set_target_properties(OpenSSL::SSL PROPERTIES",
            "    INTERFACE_INCLUDE_DIRECTORIES \"${OPENSSL_INCLUDE_DIR}\"",
        ]
    )
    if static_linkage:
        lines.extend(
            [
                "    IMPORTED_LOCATION_RELEASE \"${_openssl_prefix}/lib/libssl.lib\"",
                f'    IMPORTED_LOCATION_DEBUG "${{_openssl_prefix}}/lib/libssl{debug_postfix}.lib"',
                "    IMPORTED_LOCATION_ASAN \"${_openssl_prefix}/lib/libssl.lib\"",
            ]
        )
    else:
        lines.extend(
            [
                "    IMPORTED_IMPLIB_RELEASE \"${_openssl_prefix}/lib/libssl.lib\"",
                f'    IMPORTED_IMPLIB_DEBUG "${{_openssl_prefix}}/lib/libssl{debug_postfix}.lib"',
                "    IMPORTED_IMPLIB_ASAN \"${_openssl_prefix}/lib/libssl.lib\"",
                f'    IMPORTED_LOCATION_RELEASE "${{_openssl_prefix}}/bin/{release_ssl_dll}"',
                f'    IMPORTED_LOCATION_DEBUG "${{_openssl_prefix}}/bin/{debug_ssl_dll}"',
                f'    IMPORTED_LOCATION_ASAN "${{_openssl_prefix}}/bin/{release_ssl_dll}"',
            ]
        )
    lines.extend(
        [
            "    INTERFACE_LINK_LIBRARIES OpenSSL::Crypto",
            "    MAP_IMPORTED_CONFIG_MINSIZEREL Release",
            "    MAP_IMPORTED_CONFIG_RELWITHDEBINFO Release",
            "  )",
            "endif()",
            "",
            "if(NOT TARGET OpenSSL::applink)",
            "  add_library(OpenSSL::applink INTERFACE IMPORTED)",
            "  set_property(TARGET OpenSSL::applink PROPERTY",
            "    INTERFACE_SOURCES \"${_openssl_prefix}/include/openssl/applink.c\")",
            "endif()",
            "unset(_openssl_prefix)",
            "",
        ]
    )
    return "\n".join(lines)


def _cmake_version_text(version: str) -> str:
    return f'''set(PACKAGE_VERSION "{version}")
if(PACKAGE_FIND_VERSION VERSION_GREATER PACKAGE_VERSION)
  set(PACKAGE_VERSION_COMPATIBLE FALSE)
else()
  set(PACKAGE_VERSION_COMPATIBLE TRUE)
  if(PACKAGE_FIND_VERSION VERSION_EQUAL PACKAGE_VERSION)
    set(PACKAGE_VERSION_EXACT TRUE)
  endif()
endif()
'''


def _write_windows_package(builder, ctx) -> None:
    if builder.dry_run:
        return
    cmake_dir = ctx.install_prefix / "lib" / "cmake" / "OpenSSL"
    cmake_dir.mkdir(parents=True, exist_ok=True)
    version = _openssl_version(ctx.src_dir)
    (cmake_dir / "OpenSSLConfig.cmake").write_text(
        _windows_cmake_package_text(builder, ctx.src_dir),
        encoding="utf-8",
    )
    (cmake_dir / "OpenSSLConfigVersion.cmake").write_text(
        _cmake_version_text(version),
        encoding="utf-8",
    )


def _remove_posix_static_archives(builder, install_prefix: Path) -> None:
    # OpenSSL's `shared` configuration deliberately installs both the shared
    # objects and static development archives on Unix.  A dynamic builder mode
    # should expose only the selected shared linkage.
    if builder.dry_run or bool(builder.config.global_cfg.static_default):
        return
    for lib_dir_name in ("lib", "lib64"):
        lib_dir = install_prefix / lib_dir_name
        for name in ("libcrypto.a", "libssl.a"):
            archive = lib_dir / name
            if archive.exists():
                archive.unlink()


def build(builder, ctx, env: dict[str, str]) -> None:
    if builder.platform.os not in {"windows", "linux", "macos"}:
        raise RuntimeError(f"Unsupported platform for OpenSSL: {builder.platform.os}")
    _run_source_build(builder, ctx, env)
    if builder.platform.os == "windows":
        _copy_windows_debug_compat(builder, ctx)
        _write_windows_package(builder, ctx)
    else:
        _remove_posix_static_archives(builder, ctx.install_prefix)
