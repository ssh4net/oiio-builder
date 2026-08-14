from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import shutil
import sys

from .recipes import registry as recipe_registry
from .runner import banner, print_cmd, run
from .tooling import normalize_override, resolve_nasm_executable


def build(builder: Any, ctx: Any, env: dict[str, str]) -> None:
    build_system = ctx.repo.build_system
    if build_system == "cmake":
        _build_cmake(builder, ctx, env)
        return
    if build_system == "autotools":
        _build_autotools(builder, ctx, env)
        return
    if recipe_registry.build_backend(ctx.repo.name, builder, ctx, env):
        return
    raise RuntimeError(f"Unsupported build_system: {build_system}")


def install_only(builder: Any, ctx: Any, env: dict[str, str]) -> bool:
    build_system = ctx.repo.build_system
    if build_system == "cmake":
        return _cmake_install_only(builder, ctx, env)
    if build_system == "autotools":
        return _autotools_install_only(builder, ctx, env)
    recipe_result = recipe_registry.install_only(ctx.repo.name, builder, ctx, env)
    if recipe_result is not None:
        return recipe_result
    return False


def cmake_make_program_args(builder: Any, *, force_windows_ninja: bool = False) -> list[str]:
    if builder.platform.os != "windows":
        return []
    if not force_windows_ninja and not builder._windows_ninja_generator_active():
        return []
    ninja = builder._resolve_windows_native_ninja(builder._effective_host_env())
    return [f"-DCMAKE_MAKE_PROGRAM={builder._cmake_path_arg(ninja)}"]


def cmake_generator_args(builder: Any) -> list[str]:
    cfg = builder.config.global_cfg
    if builder.platform.os != "windows":
        return ["-G", "Ninja"]

    def _windows_vs_generator() -> str:
        # Allow overriding the Visual Studio generator name to support
        # multiple VS versions (e.g. "Visual Studio 18 2026" in CMake 4.2+).
        raw = cfg.windows.get("vs_generator")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return "Visual Studio 17 2022"

    generator = builder._windows_generator()
    if generator == "msvc":
        return ["-G", _windows_vs_generator()]
    if generator == "msvc-clang-cl":
        return ["-G", _windows_vs_generator(), "-T", "ClangCL"]
    if generator == "ninja-clang-cl":
        return ["-G", "Ninja", *cmake_make_program_args(builder)]
    # default: ninja + msvc
    return ["-G", "Ninja", *cmake_make_program_args(builder)]


def _expand_args(builder: Any, args: list[str], build_type: str, prefix: Path) -> list[str]:
    cfg = builder.config.global_cfg
    mapping = {
        "SRC_ROOT": str(cfg.src_root),
        "BUILD_TYPE": build_type,
        "PREFIX": str(prefix),
        "LIBRAW_ENABLE_EXAMPLES": cfg.libraw_enable_examples,
        "LIBRAW_ENABLE_OPENMP": cfg.libraw_enable_openmp,
        "LIBJXL_ENABLE_TOOLS": cfg.libjxl_enable_tools,
        "OPENJPEG_BUILD_CODEC": builder._resolve_openjpeg_build_codec(),
        "OCIO_BUILD_APPS": cfg.ocio_build_apps,
    }
    expanded: list[str] = []
    for arg in args:
        out = arg
        for key, value in mapping.items():
            out = out.replace(f"${{{key}}}", str(value))
        expanded.append(out)
    return expanded


def _repo_specific_args(builder: Any, repo: Any, ctx: Any) -> list[str]:
    args: list[str] = []
    args.extend(builder._repo_cmake_defaults_args(repo.name))

    recipe_args = recipe_registry.cmake_args(repo.name, builder, ctx)
    if recipe_args is not None:
        args.extend(recipe_args)
    return args


def _autotools_args(builder: Any, repo: Any) -> list[str]:
    return recipe_registry.autotools_args(repo.name, builder, repo) or []


def _autotools_linkage_args(builder: Any) -> list[str]:
    if builder.config.global_cfg.static_default:
        return ["--disable-shared", "--enable-static"]
    return ["--enable-shared", "--disable-static"]


def cmake_common_args(builder: Any, repo: Any, ctx: Any) -> list[str]:
    cfg = builder.config.global_cfg
    args: list[str] = [
        f"-DCMAKE_BUILD_TYPE={ctx.build_type}",
        f"-DCMAKE_INSTALL_PREFIX={builder._cmake_path_arg(ctx.install_prefix)}",
        f"-DCMAKE_PREFIX_PATH={builder._cmake_path_arg(ctx.install_prefix)}",
        f"-DCMAKE_INCLUDE_PATH={builder._cmake_path_arg(ctx.install_prefix / 'include')}",
        f"-DCMAKE_LIBRARY_PATH={builder._cmake_path_arg(ctx.install_prefix / 'lib')}",
        f"-DCMAKE_CXX_STANDARD={repo.cxx_standard or cfg.cxx_standard}",
        f"-DCMAKE_CXX_EXTENSIONS={'ON' if cfg.cxx_extensions else 'OFF'}",
        f"-DPKG_CONFIG_USE_STATIC_LIBS={'ON' if cfg.static_default else 'OFF'}",
    ]
    if builder._ccache_path:
        args.append(f"-DCMAKE_C_COMPILER_LAUNCHER={builder._cmake_path_arg(builder._ccache_path)}")
        args.append(f"-DCMAKE_CXX_COMPILER_LAUNCHER={builder._cmake_path_arg(builder._ccache_path)}")

    if builder.platform.os == "windows":
        # MSBuild + VS generators sometimes hit file timestamp races in the generated
        # "check build system" custom steps (generate.stamp). The builder always
        # re-configures from scratch when it rebuilds a repo, so regeneration is
        # unnecessary here.
        generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
        if generator in {"msvc", "msvc-clang-cl"}:
            args.append("-DCMAKE_SUPPRESS_REGENERATION=ON")
        effective_env = builder._effective_host_env()
        rc_compiler = builder._resolve_windows_sdk_tool("rc.exe", effective_env)
        mt_tool = builder._resolve_windows_sdk_tool("mt.exe", effective_env)
        if rc_compiler:
            args.append(f"-DCMAKE_RC_COMPILER={builder._cmake_path_arg(rc_compiler)}")
        if mt_tool:
            args.append(f"-DCMAKE_MT={builder._cmake_path_arg(mt_tool)}")

    pkg_cfg = normalize_override(cfg.env.get("PKG_CONFIG_EXECUTABLE") or cfg.env.get("PKG_CONFIG"))
    if builder.platform.os == "windows":
        pkg_cfg = normalize_override(
            cfg.windows_env.get("PKG_CONFIG_EXECUTABLE")
            or cfg.windows_env.get("PKG_CONFIG")
            or os.environ.get("PKG_CONFIG_EXECUTABLE")
            or os.environ.get("PKG_CONFIG")
        ) or pkg_cfg
    else:
        pkg_cfg = normalize_override(os.environ.get("PKG_CONFIG_EXECUTABLE") or os.environ.get("PKG_CONFIG")) or pkg_cfg
    if pkg_cfg:
        args.append(f"-DPKG_CONFIG_EXECUTABLE={builder._cmake_path_arg(pkg_cfg)}")

    doxygen = normalize_override(cfg.env.get("DOXYGEN_EXECUTABLE"))
    if builder.platform.os == "windows":
        doxygen = normalize_override(cfg.windows_env.get("DOXYGEN_EXECUTABLE") or os.environ.get("DOXYGEN_EXECUTABLE")) or doxygen
    else:
        doxygen = normalize_override(os.environ.get("DOXYGEN_EXECUTABLE")) or doxygen
    if doxygen:
        args.append(f"-DDOXYGEN_EXECUTABLE={builder._cmake_path_arg(doxygen)}")

    effective_env = dict(os.environ)
    effective_env.update(cfg.env)
    if builder.platform.os == "windows":
        effective_env.update(cfg.windows_env)
    nasm = resolve_nasm_executable(effective_env, platform_os=builder.platform.os)
    if nasm:
        args.append(f"-DCMAKE_ASM_NASM_COMPILER={builder._cmake_path_arg(nasm)}")

    python_exec = normalize_override(
        cfg.env.get("Python3_EXECUTABLE")
        or cfg.env.get("PYTHON3_EXECUTABLE")
        or cfg.env.get("Python_EXECUTABLE")
        or cfg.env.get("PYTHON_EXECUTABLE")
    )
    if builder.platform.os == "windows":
        python_exec = normalize_override(
            cfg.windows_env.get("Python3_EXECUTABLE")
            or cfg.windows_env.get("PYTHON3_EXECUTABLE")
            or cfg.windows_env.get("Python_EXECUTABLE")
            or cfg.windows_env.get("PYTHON_EXECUTABLE")
            or os.environ.get("Python3_EXECUTABLE")
            or os.environ.get("PYTHON3_EXECUTABLE")
            or os.environ.get("Python_EXECUTABLE")
            or os.environ.get("PYTHON_EXECUTABLE")
        ) or python_exec
    else:
        python_exec = normalize_override(
            os.environ.get("Python3_EXECUTABLE")
            or os.environ.get("PYTHON3_EXECUTABLE")
            or os.environ.get("Python_EXECUTABLE")
            or os.environ.get("PYTHON_EXECUTABLE")
        ) or python_exec

    cpython_enabled = builder._cpython_enabled_for_run()
    if cpython_enabled:
        prefix_posix = ctx.install_prefix.as_posix()
        args.append(f"-DPython3_ROOT_DIR={prefix_posix}")
        args.append(f"-DPython_ROOT_DIR={prefix_posix}")
        args.append("-DPython3_FIND_STRATEGY=LOCATION")
        args.append("-DPython_FIND_STRATEGY=LOCATION")
        if not python_exec:
            prefix_python = builder._prefix_python_executable(ctx.install_prefix, ctx.build_type)
            if prefix_python is not None:
                python_exec = prefix_python.as_posix()
        if builder.platform.os == "windows" and not python_exec:
            python_exec = builder._host_python_executable_for_prefix(ctx.install_prefix)

    # Keep Python resolution portable by default:
    # - do not hardcode an absolute interpreter path unless user-provided;
    # - prefer PATH/venv over Windows registry-provided interpreters.
    if builder.platform.os == "windows":
        args.append("-DPython3_FIND_REGISTRY=NEVER")
        args.append("-DPython_FIND_REGISTRY=NEVER")

    in_virtual_env = (
        bool(os.environ.get("VIRTUAL_ENV"))
        or bool(os.environ.get("CONDA_PREFIX"))
        or bool(getattr(sys, "real_prefix", ""))
        or (getattr(sys, "base_prefix", sys.prefix) != sys.prefix)
    )
    if in_virtual_env:
        args.append("-DPython3_FIND_VIRTUALENV=ONLY")
        args.append("-DPython_FIND_VIRTUALENV=ONLY")

    if python_exec:
        python_exec_arg = builder._cmake_path_arg(python_exec)
        args.append(f"-DPython3_EXECUTABLE={python_exec_arg}")
        args.append(f"-DPython_EXECUTABLE={python_exec_arg}")

    if builder.platform.os == "windows" and cpython_enabled:
        python_release_lib, python_debug_lib = builder._prefix_windows_python_libraries(ctx.install_prefix)
        if python_release_lib is not None:
            release_posix = python_release_lib.as_posix()
            args.append(f"-DPython3_LIBRARY_RELEASE={release_posix}")
            args.append(f"-DPython_LIBRARY_RELEASE={release_posix}")
        if python_debug_lib is not None:
            debug_posix = python_debug_lib.as_posix()
            args.append(f"-DPython3_LIBRARY_DEBUG={debug_posix}")
            args.append(f"-DPython_LIBRARY_DEBUG={debug_posix}")
        if python_release_lib is not None or python_debug_lib is not None:
            if ctx.build_type == "Debug":
                chosen = python_debug_lib or python_release_lib
            else:
                chosen = python_release_lib
            if chosen is not None:
                chosen_posix = chosen.as_posix()
                args.append(f"-DPython3_LIBRARY={chosen_posix}")
                args.append(f"-DPython_LIBRARY={chosen_posix}")

    if cfg.pic:
        args.append("-DCMAKE_POSITION_INDEPENDENT_CODE=ON")

    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        args.append(f"-DCMAKE_DEBUG_POSTFIX={debug_postfix}")
        args.append("-DCMAKE_POLICY_DEFAULT_CMP0091=NEW")
        runtime_mode = builder._windows_runtime_mode()
        if runtime_mode == "static":
            runtime = "MultiThreaded$<$<CONFIG:Debug>:Debug>"
        elif runtime_mode == "dynamic":
            runtime = "MultiThreaded$<$<CONFIG:Debug>:Debug>DLL"
        else:
            runtime = str(cfg.windows.get("msvc_runtime"))
        args.append(f"-DCMAKE_MSVC_RUNTIME_LIBRARY={runtime}")

    if repo.shared is None:
        build_shared = not cfg.static_default
    else:
        build_shared = repo.shared
    args.append(f"-DBUILD_SHARED_LIBS={'ON' if build_shared else 'OFF'}")

    cflags = builder._base_flags(ctx.build_type)
    cxxflags = builder._base_flags(ctx.build_type)
    if builder.license_profile is not None:
        profile_definitions = builder.license_profile.consumer_compile_definitions
        if profile_definitions:
            define_flag = "/D" if builder.platform.os == "windows" else "-D"
            cxxflags += " " + " ".join(f"{define_flag}{item}" for item in profile_definitions)
    if builder.platform.os == "windows":
        cxxflags += " /bigobj"
    if builder.platform.os in {"macos", "linux"} and cfg.use_libcxx:
        cxxflags += " -stdlib=libc++"

    if ctx.build_type == "ASAN":
        if builder.platform.os == "windows":
            cxxflags += " /fsanitize=address"
            cflags += " /fsanitize=address"
        else:
            cxxflags += " -fsanitize=address -fno-omit-frame-pointer"
            cflags += " -fsanitize=address -fno-omit-frame-pointer"
    args.append(f"-DCMAKE_C_FLAGS_INIT={cflags}")
    args.append(f"-DCMAKE_CXX_FLAGS_INIT={cxxflags}")

    linker_flags = builder._linker_flags_init()
    if linker_flags:
        args += [
            f"-DCMAKE_EXE_LINKER_FLAGS_INIT={linker_flags}",
            f"-DCMAKE_SHARED_LINKER_FLAGS_INIT={linker_flags}",
            f"-DCMAKE_MODULE_LINKER_FLAGS_INIT={linker_flags}",
        ]

    if builder.toolchain and (builder.platform.os != "windows" or builder._windows_should_pin_cmake_compiler()):
        if "cc" in builder.toolchain:
            args.append(f"-DCMAKE_C_COMPILER={builder._cmake_path_arg(builder.toolchain['cc'])}")
        if "cxx" in builder.toolchain:
            args.append(f"-DCMAKE_CXX_COMPILER={builder._cmake_path_arg(builder.toolchain['cxx'])}")
        if "ld" in builder.toolchain:
            args.append(f"-DCMAKE_LINKER={builder._cmake_path_arg(builder.toolchain['ld'])}")
        if "ar" in builder.toolchain:
            args.append(f"-DCMAKE_AR={builder._cmake_path_arg(builder.toolchain['ar'])}")
        if "ranlib" in builder.toolchain:
            args.append(f"-DCMAKE_RANLIB={builder._cmake_path_arg(builder.toolchain['ranlib'])}")

    return args


def _build_cmake(builder: Any, ctx: Any, env: dict[str, str]) -> None:
    repo = ctx.repo
    build_dir = ctx.build_dir
    if not builder.dry_run:
        cache = build_dir / "CMakeCache.txt"
        if cache.exists():
            shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["cmake", "-S", str(ctx.src_dir), "-B", str(build_dir)]
    cmd.extend(cmake_generator_args(builder))

    cmake_args = cmake_common_args(builder, repo, ctx)
    cmake_args.extend(_repo_specific_args(builder, repo, ctx))
    cmake_args.extend(_expand_args(builder, repo.cmake_args, ctx.build_type, ctx.install_prefix))
    cmake_args.extend(builder._repo_cmake_user_override_args(repo.name))
    # License profile guards are intentionally last so a local CMake override
    # cannot re-enable an excluded artifact path by accident.
    cmake_args.extend(builder._license_profile_cmake_args(repo.name))
    cmd.extend(cmake_args)

    print_cmd("Full cmake config command", cmd)
    banner(f"{repo.name} ({ctx.build_type}) - configure")
    run(cmd, env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "configure")))

    build_cmd = ["cmake", "--build", str(build_dir), "--config", ctx.build_type, "--parallel", str(builder._jobs())]
    print_cmd("build command", build_cmd)
    banner(f"{repo.name} ({ctx.build_type}) - building")
    run(build_cmd, env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "build")))

    install_cmd = ["cmake", "--install", str(build_dir), "--config", ctx.build_type]
    print_cmd("install command", install_cmd)
    banner(f"{repo.name} ({ctx.build_type}) - install")
    run(install_cmd, env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "install")))


def _build_autotools(builder: Any, ctx: Any, env: dict[str, str]) -> None:
    repo = ctx.repo
    _prepare_autotools_build_dir(builder, ctx)
    configure = ctx.src_dir / "configure"
    if not builder.dry_run and not configure.exists():
        raise RuntimeError(f"Missing configure script for {repo.name}: {configure}")
    use_msys2_autotools = _autotools_windows_msys2_active(builder)
    if builder.platform.os == "windows" and not use_msys2_autotools:
        raise RuntimeError(
            f"{repo.name}: Windows autotools builds require MSYS2 shell/tools in PATH "
            "(MSYSTEM set, plus bash+make)."
        )
    prefix_arg = ctx.install_prefix.as_posix() if use_msys2_autotools else str(ctx.install_prefix)
    env = _autotools_build_env(builder, ctx, env)
    configure_args = [f"--prefix={prefix_arg}", *_autotools_linkage_args(builder), *_autotools_args(builder, repo)]
    cmd = _autotools_configure_command(builder, configure, configure_args, env)
    print_cmd("configure command", cmd)
    banner(f"{repo.name} ({ctx.build_type}) - configure")
    run(
        cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "configure")),
    )

    build_cmd = _autotools_make_command(builder, [f"-j{builder._jobs()}"], env)
    print_cmd("build command", build_cmd)
    banner(f"{repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "build")),
    )

    install_cmd = _autotools_make_command(builder, ["install"], env)
    print_cmd("install command", install_cmd)
    banner(f"{repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(repo.name, ctx.build_type, "install")),
    )


def _prepare_autotools_build_dir(builder: Any, ctx: Any) -> None:
    if not builder.dry_run and ctx.build_dir.exists():
        shutil.rmtree(ctx.build_dir, ignore_errors=True)
    ctx.build_dir.mkdir(parents=True, exist_ok=True)


def _autotools_build_env(builder: Any, ctx: Any, env: dict[str, str]) -> dict[str, str]:
    use_msys2_autotools = _autotools_windows_msys2_active(builder)
    cflags, cxxflags, ldflags = builder._non_cmake_flags(ctx.build_type)
    if builder.platform.os in {"linux", "macos"} and builder.config.global_cfg.use_libcxx:
        ldflags = ldflags.replace("-stdlib=libc++", "").strip()

    include_dir = ctx.install_prefix / "include"
    lib_dir = ctx.install_prefix / "lib"
    include_arg = include_dir.as_posix() if use_msys2_autotools else str(include_dir)
    lib_arg = lib_dir.as_posix() if use_msys2_autotools else str(lib_dir)

    result = {
        **env,
        "CFLAGS": f"{cflags} -I{include_arg}",
        "CXXFLAGS": f"{cxxflags} -I{include_arg}",
        "LDFLAGS": f"{ldflags} -L{lib_arg}".strip(),
        "CPPFLAGS": f"-I{include_arg}",
    }
    if builder.platform.os != "windows":
        if "cc" in builder.toolchain:
            result["CC"] = builder.toolchain["cc"]
        if "cxx" in builder.toolchain:
            result["CXX"] = builder.toolchain["cxx"]
        if "ar" in builder.toolchain:
            result["AR"] = builder.toolchain["ar"]
        if "ranlib" in builder.toolchain:
            result["RANLIB"] = builder.toolchain["ranlib"]
    return result


def _autotools_windows_msys2_active(builder: Any) -> bool:
    return builder.platform.os == "windows" and builder._windows_msys2_detected()


def _autotools_configure_command(builder: Any, configure: Path, args: list[str], env: dict[str, str]) -> list[str]:
    if not _autotools_windows_msys2_active(builder):
        return [str(configure), *args]

    shell = builder._resolve_windows_posix_shell(env)
    if not shell:
        raise RuntimeError(
            "Windows autotools builds require an MSYS2 POSIX shell (bash/sh) in PATH. "
            "Run from an MSYS2 shell (MSYSTEM set)."
        )
    return [shell, configure.as_posix(), *args]


def _autotools_make_command(builder: Any, make_args: list[str], env: dict[str, str]) -> list[str]:
    if not _autotools_windows_msys2_active(builder):
        return ["make", *make_args]

    make = builder._which_in_env("make", env) or builder._which_in_env("mingw32-make", env)
    if not make:
        raise RuntimeError(
            "Windows autotools builds require MSYS2 make in PATH. "
            "Run from an MSYS2 shell (MSYSTEM set)."
        )
    return [make, *make_args]


def _cmake_cache_value(cache_path: Path, key: str) -> str | None:
    try:
        text = cache_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = f"{key}:"
    for line in text.splitlines():
        if not line or line.startswith(("//", "#")):
            continue
        if not line.startswith(prefix):
            continue
        _, _, value = line.partition("=")
        value = value.strip()
        return value or None
    return None


def _cmake_cache_vars_referencing_prefix(cache_path: Path, prefix: str) -> list[str]:
    prefix = prefix.strip()
    if not prefix:
        return []

    needle = prefix.replace("\\", "/")
    needle_norm = needle.rstrip("/")
    needles = {needle, needle_norm} if needle_norm else {needle}

    try:
        lines = cache_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    hits: set[str] = set()
    for line in lines:
        if not line or line.startswith(("//", "#")):
            continue
        colon = line.find(":")
        if colon <= 0:
            continue
        eq = line.find("=", colon + 1)
        if eq <= colon:
            continue
        name = line[:colon].strip()
        if not name:
            continue
        value = line[eq + 1 :].strip()
        if not value:
            continue
        value_norm = value.replace("\\", "/")
        if any(n in value_norm for n in needles):
            hits.add(name)

    return sorted(hits)


def _cmake_install_only(builder: Any, ctx: Any, env: dict[str, str]) -> bool:
    if not (ctx.build_dir / "cmake_install.cmake").exists():
        return False
    cache_path = ctx.build_dir / "CMakeCache.txt"
    if not cache_path.exists():
        return False

    cached_prefix = _cmake_cache_value(cache_path, "CMAKE_INSTALL_PREFIX")
    desired_prefix = os.path.normcase(os.path.normpath(str(ctx.install_prefix)))
    cached_prefix_norm = os.path.normcase(os.path.normpath(cached_prefix)) if cached_prefix else ""

    if cached_prefix_norm and cached_prefix_norm != desired_prefix:
        cmd = ["cmake", "-S", str(ctx.src_dir), "-B", str(ctx.build_dir)]
        cmd.extend(cmake_generator_args(builder))
        stale_cache_vars = _cmake_cache_vars_referencing_prefix(cache_path, cached_prefix or "")
        if stale_cache_vars:
            print(
                f"[note] {ctx.repo.name} ({ctx.build_type}) prefix changed; clearing {len(stale_cache_vars)} stale CMake cache entries",
                flush=True,
            )
            for name in stale_cache_vars:
                cmd.extend(["-U", name])
        cmake_args = cmake_common_args(builder, ctx.repo, ctx)
        cmake_args.extend(_repo_specific_args(builder, ctx.repo, ctx))
        cmake_args.extend(_expand_args(builder, ctx.repo.cmake_args, ctx.build_type, ctx.install_prefix))
        cmake_args.extend(builder._repo_cmake_user_override_args(ctx.repo.name))
        cmd.extend(cmake_args)
        print_cmd("Full cmake config command", cmd)
        banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
        run(
            cmd,
            env=env,
            dry_run=builder.dry_run,
            log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
        )

    install_cmd = ["cmake", "--install", str(ctx.build_dir), "--config", ctx.build_type, "--prefix", str(ctx.install_prefix)]
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )
    return True


def _autotools_install_only(builder: Any, ctx: Any, env: dict[str, str]) -> bool:
    if not (ctx.build_dir / "Makefile").exists():
        return False
    configure = ctx.src_dir / "configure"
    if not configure.exists():
        return False
    use_msys2_autotools = _autotools_windows_msys2_active(builder)
    if builder.platform.os == "windows" and not use_msys2_autotools:
        return False
    prefix_arg = ctx.install_prefix.as_posix() if use_msys2_autotools else str(ctx.install_prefix)
    install_env = _autotools_build_env(builder, ctx, env)
    configure_args = [
        f"--prefix={prefix_arg}",
        *_autotools_linkage_args(builder),
        *_autotools_args(builder, ctx.repo),
    ]
    cmd = _autotools_configure_command(builder, configure, configure_args, install_env)
    print_cmd("configure command", cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
    run(
        cmd,
        cwd=str(ctx.build_dir),
        env=install_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
    )

    install_cmd = _autotools_make_command(builder, ["install"], install_env)
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(ctx.build_dir),
        env=install_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )
    return True
