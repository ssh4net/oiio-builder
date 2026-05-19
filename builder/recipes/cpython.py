from __future__ import annotations

from pathlib import Path
import os
import re
import shutil

from .policy import cpython_requested
from ..runner import banner, print_cmd, run


STAMP_REVISION = "4"


def enabled(builder, _repo) -> bool:
    return cpython_requested(builder)


def _windows_debug_postfix_artifact(path: Path, debug_postfix: str) -> bool:
    stem = path.stem.lower()
    postfix = debug_postfix.lower()
    if stem.endswith(f"_{postfix}"):
        return True
    if stem.startswith("vcruntime") and stem.endswith(postfix):
        return True
    return path.suffix.lower() == ".lib" and stem.startswith("python") and stem.endswith(postfix)


def _windows_python_debug_import_name(path: Path, debug_postfix: str) -> str:
    stem = path.stem
    if re.fullmatch(r"python\d{2,}", stem.lower()):
        return f"{stem}_{debug_postfix}{path.suffix}"
    return f"{stem}{debug_postfix}{path.suffix}"


def build(builder, ctx, env: dict[str, str]) -> None:
    if builder.platform.os == "windows":
        _build_windows(builder, ctx, env)
    elif builder.platform.os in {"linux", "macos"}:
        _build_posix(builder, ctx, env)
    else:
        raise RuntimeError(f"Unsupported platform for cpython build: {builder.platform.os}")


def _clean_posix_source_artifacts(builder, src_dir: Path) -> None:
    if builder.platform.os == "windows":
        return

    artifacts = [
        src_dir / "python",
        src_dir / "_bootstrap_python",
        src_dir / "Programs" / "python.o",
        src_dir / "Python" / "frozen_modules" / "MANIFEST",
    ]
    artifacts.extend(sorted((src_dir / "Python" / "frozen_modules").glob("*.h")))

    removed: list[str] = []
    for artifact in artifacts:
        if not artifact.exists() or not artifact.is_file():
            continue
        rel = str(artifact.relative_to(src_dir))
        removed.append(rel)
        if not builder.dry_run:
            artifact.unlink()

    if removed:
        preview = ", ".join(removed[:3])
        if len(removed) > 3:
            preview += f", +{len(removed) - 3} more"
        prefix = "[dry-run]" if builder.dry_run else "[note]"
        print(f"{prefix} cpython: remove source-tree build artifacts before out-of-tree build: {preview}", flush=True)


def _windows_fetch_externals(builder) -> bool:
    if builder.platform.os != "windows":
        return False
    raw = builder.config.global_cfg.windows.get("cpython_fetch_externals")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in {"1", "true", "on", "yes"}:
        return True
    if value in {"0", "false", "off", "no"}:
        return False
    return True


def _build_posix(builder, ctx, env: dict[str, str]) -> None:
    build_dir = ctx.build_dir
    src_dir = ctx.src_dir
    install_prefix = ctx.install_prefix
    configure = src_dir / "configure"
    if not configure.exists():
        raise RuntimeError(f"Missing configure script for cpython: {configure}")
    builder._normalize_posix_shell_scripts(
        ctx.repo.name,
        [
            configure,
            src_dir / "config.guess",
            src_dir / "config.sub",
            src_dir / "install-sh",
            src_dir / "pyconfig.h.in",
            src_dir / "Makefile.pre.in",
            src_dir / "Misc" / "python.pc.in",
            src_dir / "Misc" / "python-embed.pc.in",
            src_dir / "Misc" / "python-config.sh.in",
            src_dir / "Modules" / "makesetup",
            src_dir / "Modules" / "Setup",
            src_dir / "Modules" / "Setup.bootstrap.in",
            src_dir / "Modules" / "Setup.stdlib.in",
        ],
    )
    _clean_posix_source_artifacts(builder, src_dir)

    if not builder.dry_run and (build_dir / "Makefile").exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True, exist_ok=True)

    cflags, cxxflags, ldflags = builder._non_cmake_flags(ctx.build_type)
    py_env = dict(env)
    if "cc" in builder.toolchain:
        py_env["CC"] = builder.toolchain["cc"]
    if "cxx" in builder.toolchain:
        py_env["CXX"] = builder.toolchain["cxx"]
    if "ar" in builder.toolchain:
        py_env["AR"] = builder.toolchain["ar"]
    if "ranlib" in builder.toolchain:
        py_env["RANLIB"] = builder.toolchain["ranlib"]
    if cflags:
        py_env["CFLAGS"] = cflags
    if cxxflags:
        py_env["CXXFLAGS"] = cxxflags
    if ldflags:
        py_env["LDFLAGS"] = ldflags
    if (install_prefix / "lib" / "pkgconfig" / "sqlite3.pc").exists():
        py_env.setdefault("LIBSQLITE3_CFLAGS", f"-I{(install_prefix / 'include').as_posix()}")
        py_env.setdefault("LIBSQLITE3_LIBS", f"-L{(install_prefix / 'lib').as_posix()} -lsqlite3 -lz -lm")

    configure_cmd = [
        str(configure),
        f"--prefix={install_prefix}",
        "--enable-shared",
        "--without-ensurepip",
    ]
    print_cmd("configure command", configure_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
    run(
        configure_cmd,
        cwd=str(build_dir),
        env=py_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
    )

    build_cmd = ["make", f"-j{builder._jobs()}"]
    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(build_dir),
        env=py_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )

    install_cmd = ["make", "install"]
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(build_dir),
        env=py_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )


def _build_windows(builder, ctx, env: dict[str, str]) -> None:
    src_dir = ctx.src_dir
    install_prefix = ctx.install_prefix
    build_script = src_dir / "PCbuild" / "build.bat"
    if not build_script.exists():
        raise RuntimeError(f"Missing CPython Windows build script: {build_script}")

    if builder.platform.arch == "x86_64":
        pcbuild_platform = "x64"
        output_dirs = ["amd64", "x64"]
    elif builder.platform.arch == "arm64":
        pcbuild_platform = "arm64"
        output_dirs = ["arm64", "ARM64"]
    else:
        raise RuntimeError(f"Unsupported Windows architecture for cpython: {builder.platform.arch}")

    config_name = "Debug" if ctx.build_type == "Debug" else "Release"
    fetch_externals = _windows_fetch_externals(builder)

    builder._ensure_zlib_windows_alias(install_prefix, ctx.build_type)
    builder._ensure_bzip2_alias(install_prefix, ctx.build_type)

    py_env = dict(env)
    for var in ("CL", "_CL_", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "LINK"):
        py_env[var] = ""
    py_env["_CL_"] = "/Y-"
    py_env.setdefault("PATH", os.environ.get("PATH", ""))
    py_env.setdefault("INCLUDE", os.environ.get("INCLUDE", ""))
    py_env.setdefault("LIB", os.environ.get("LIB", ""))
    py_env.setdefault("LIBPATH", os.environ.get("LIBPATH", ""))

    include_dir = install_prefix / "include"
    lib_dir = install_prefix / "lib"
    bin_dir = install_prefix / "bin"
    if include_dir.is_dir():
        builder._prepend_windows_env_paths(py_env, "INCLUDE", [include_dir])
    if lib_dir.is_dir():
        builder._prepend_windows_env_paths(py_env, "LIB", [lib_dir])
        builder._prepend_windows_env_paths(py_env, "LIBPATH", [lib_dir])
    if bin_dir.is_dir():
        builder._prepend_windows_env_paths(py_env, "PATH", [bin_dir])

    if not builder.dry_run:
        pcbuild_root = src_dir / "PCbuild"
        for name in output_dirs:
            candidate = pcbuild_root / name
            if candidate.exists():
                shutil.rmtree(candidate, ignore_errors=True)
        obj_dir = pcbuild_root / "obj"
        if obj_dir.exists():
            shutil.rmtree(obj_dir, ignore_errors=True)

    build_cmd = [
        "cmd",
        "/c",
        str(build_script),
        "-p",
        pcbuild_platform,
        "-c",
        config_name,
        "--no-tkinter",
    ]
    build_cmd.append("-e" if fetch_externals else "-E")
    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(
        build_cmd,
        cwd=str(src_dir),
        env=py_env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")),
    )

    if builder.dry_run:
        return

    pcbuild_root = src_dir / "PCbuild"
    output_dir: Path | None = None
    for name in output_dirs:
        candidate = pcbuild_root / name
        if candidate.is_dir() and list(candidate.glob("python*.lib")):
            output_dir = candidate
            break
    if output_dir is None:
        for candidate in sorted(pcbuild_root.iterdir()):
            if candidate.is_dir() and list(candidate.glob("python*.lib")):
                output_dir = candidate
                break
    if output_dir is None:
        raise RuntimeError(f"Could not locate CPython build output under: {pcbuild_root}")

    include_dst = install_prefix / "include"
    lib_dst = install_prefix / "lib"
    libs_compat_dst = install_prefix / "libs"
    bin_dst = install_prefix / "bin"
    dlls_dst = install_prefix / "DLLs"
    include_dst.mkdir(parents=True, exist_ok=True)
    lib_dst.mkdir(parents=True, exist_ok=True)
    libs_compat_dst.mkdir(parents=True, exist_ok=True)
    bin_dst.mkdir(parents=True, exist_ok=True)
    dlls_dst.mkdir(parents=True, exist_ok=True)
    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))

    stale_specs = [
        (lib_dst, "python*.lib"),
        (libs_compat_dst, "python*.lib"),
        (install_prefix, "python*.dll"),
        (install_prefix, "python*.exe"),
        (install_prefix, "python*.pdb"),
        (install_prefix, "vcruntime*.dll"),
        (install_prefix, "vcruntime*.pdb"),
        (bin_dst, "python*.dll"),
        (bin_dst, "python*.exe"),
        (bin_dst, "python*.pdb"),
        (bin_dst, "vcruntime*.dll"),
        (bin_dst, "vcruntime*.pdb"),
        (dlls_dst, "*.pyd"),
        (dlls_dst, "*.dll"),
        (dlls_dst, "*.pdb"),
    ]
    for directory, pattern in stale_specs:
        for stale in directory.glob(pattern):
            is_debug_artifact = _windows_debug_postfix_artifact(stale, debug_postfix)
            if (ctx.build_type == "Debug") == is_debug_artifact:
                stale.unlink()

    include_src = src_dir / "Include"
    if include_src.is_dir():
        shutil.copytree(include_src, include_dst, dirs_exist_ok=True)
    pyconfig_dst = include_dst / "pyconfig.h"
    if pyconfig_dst.exists():
        pyconfig_dst.unlink()
    pyconfig_candidates = (
        output_dir / "pyconfig.h",
        src_dir / "PCbuild" / "pyconfig.h",
        src_dir / "PC" / "pyconfig.h",
        src_dir / "PC" / "pyconfig.h.in",
    )
    for pyconfig_candidate in pyconfig_candidates:
        if pyconfig_candidate.exists():
            shutil.copy2(pyconfig_candidate, pyconfig_dst)
            break
    if not pyconfig_dst.exists():
        raise RuntimeError(f"Could not locate generated CPython pyconfig.h under: {src_dir / 'PCbuild'}")

    for lib_file in sorted(output_dir.glob("python*.lib")):
        shutil.copy2(lib_file, lib_dst / lib_file.name)
        shutil.copy2(lib_file, libs_compat_dst / lib_file.name)

    for pattern in ("python*.dll", "python*.exe", "python*.pdb"):
        for file_path in sorted(output_dir.glob(pattern)):
            shutil.copy2(file_path, bin_dst / file_path.name)
    for pattern in ("vcruntime*.dll", "vcruntime*.pdb"):
        for file_path in sorted(output_dir.glob(pattern)):
            shutil.copy2(file_path, bin_dst / file_path.name)

    dlls_members: list[Path] = []
    dlls_members.extend(sorted(output_dir.glob("*.pyd")))
    for dll_file in sorted(output_dir.glob("*.dll")):
        stem = dll_file.stem.lower()
        if stem.startswith("python") or stem.startswith("vcruntime") or stem == "pyshellext":
            continue
        dlls_members.append(dll_file)
    for file_path in dlls_members:
        shutil.copy2(file_path, dlls_dst / file_path.name)
        pdb_file = file_path.with_suffix(".pdb")
        if pdb_file.exists():
            shutil.copy2(pdb_file, dlls_dst / pdb_file.name)

    license_src = output_dir / "LICENSE.txt"
    if license_src.exists():
        shutil.copy2(license_src, install_prefix / "LICENSE.txt")

    stdlib_src = src_dir / "Lib"
    if stdlib_src.is_dir():
        shutil.copytree(stdlib_src, install_prefix / "Lib", dirs_exist_ok=True)

    debug_libs = list(lib_dst.glob(f"python*{debug_postfix}.lib")) + list(lib_dst.glob(f"python*_{debug_postfix}.lib"))
    if ctx.build_type == "Debug" and not debug_libs:
        release_libs = [p for p in sorted(lib_dst.glob("python*.lib")) if not p.name.lower().endswith(f"{debug_postfix}.lib")]
        if release_libs:
            version_stem = builder._prefix_python_lib_stem(install_prefix)
            versioned_release_libs = [p for p in release_libs if version_stem and p.stem.lower() == version_stem]
            source = versioned_release_libs[0] if versioned_release_libs else release_libs[0]
            fallback_name = _windows_python_debug_import_name(source, debug_postfix)
            shutil.copy2(source, lib_dst / fallback_name)
            shutil.copy2(source, libs_compat_dst / fallback_name)
