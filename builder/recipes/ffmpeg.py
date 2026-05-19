from __future__ import annotations

from pathlib import Path
import os
import shutil

from .policy import ffmpeg_enabled, imageio_enabled, windows_ffmpeg_native_build_enabled
from ..runner import banner, print_cmd, run


def enabled(builder, _repo) -> bool:
    if builder.platform.os == "windows":
        if windows_ffmpeg_native_build_enabled(builder):
            return bool(ffmpeg_enabled(builder)) and imageio_enabled(builder)
        if ffmpeg_enabled(builder) and not builder.dry_run:
            print(
                "[skip] ffmpeg: native build step is disabled on Windows; "
                "run from an MSYS2 shell (MSYSTEM set) to build from source, "
                "otherwise prebuilt FFmpeg is consumed via FFmpeg_ROOT/FFMPEG_ROOT or <src_root>/ffmpeg",
                flush=True,
            )
        return False
    if not imageio_enabled(builder):
        return False
    return bool(ffmpeg_enabled(builder))


def _configure_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    windows_native_ffmpeg = builder.platform.os == "windows" and windows_ffmpeg_native_build_enabled(builder)
    prefix_arg = builder._windows_path_to_msys(ctx.install_prefix) if windows_native_ffmpeg else str(ctx.install_prefix)
    args = [
        f"--prefix={prefix_arg}",
        "--disable-shared",
        "--enable-static",
        "--enable-pic",
        "--disable-doc",
        "--pkg-config-flags=--static",
    ]
    if windows_native_ffmpeg:
        target_os = "win64" if builder.platform.arch in {"x86_64", "arm64"} else "win32"
        ffmpeg_arch = (
            "aarch64"
            if builder.platform.arch == "arm64"
            else ("x86_64" if builder.platform.arch == "x86_64" else builder.platform.arch)
        )
        args.extend(
            [
                f"--target-os={target_os}",
                f"--arch={ffmpeg_arch}",
                "--toolchain=msvc",
                "--ar=lib",
                "--ranlib=:",
                "--disable-unstable",
            ]
        )
        generator = str(cfg.windows.get("generator", "ninja-msvc")).strip().lower()
        if generator in {"msvc-clang-cl", "ninja-clang-cl"}:
            args.append("--cc=clang-cl")
            args.append("--cxx=clang-cl")
        else:
            args.append("--cc=cl")
            args.append("--cxx=cl")
    if ctx.build_type == "Release":
        if not windows_native_ffmpeg:
            args.append("--disable-debug")
    else:
        if windows_native_ffmpeg:
            debug_postfix = str(cfg.windows.get("debug_postfix", "d")).strip()
            args.append("--enable-debug")
            if debug_postfix:
                args.append(f"--build-suffix={debug_postfix}")
        else:
            args.append("--enable-debug=3")

    if "cc" in builder.toolchain and not windows_native_ffmpeg:
        args.append(f"--cc={builder.toolchain['cc']}")
    if "cxx" in builder.toolchain and not windows_native_ffmpeg:
        args.append(f"--cxx={builder.toolchain['cxx']}")
    if "ar" in builder.toolchain and not windows_native_ffmpeg:
        args.append(f"--ar={builder.toolchain['ar']}")
    if "ranlib" in builder.toolchain and not windows_native_ffmpeg:
        args.append(f"--ranlib={builder.toolchain['ranlib']}")
    if builder.platform.os == "macos":
        sdkroot = builder.toolchain.get("sdkroot")
        if sdkroot:
            args.append(f"--sysroot={sdkroot}")

    if windows_native_ffmpeg:
        runtime_flag = "-MTd" if ctx.build_type == "Debug" else "-MT"
        args.append(f"--extra-cflags={runtime_flag}")
        args.append(f"--extra-cxxflags={runtime_flag}")
    else:
        cflags, cxxflags, ldflags = builder._non_cmake_flags(ctx.build_type)
        include_dir = ctx.install_prefix / "include"
        lib_dir = ctx.install_prefix / "lib"
        cflags = f"{cflags} -I{include_dir}"
        cxxflags = f"{cxxflags} -I{include_dir}"
        ldflags = f"{ldflags} -L{lib_dir}"
        args.append(f"--extra-cflags={cflags}")
        args.append(f"--extra-cxxflags={cxxflags}")
        args.append(f"--extra-ldflags={ldflags}")
    return args


def _configure_command(builder, configure: Path, args: list[str], env: dict[str, str]) -> list[str]:
    if builder.platform.os != "windows":
        return [str(configure), *args]
    if not windows_ffmpeg_native_build_enabled(builder):
        return [str(configure), *args]

    shell = builder._resolve_windows_posix_shell(env)
    if not shell:
        raise RuntimeError(
            "FFmpeg native build on Windows requires an MSYS2 POSIX shell (bash/sh) in PATH. "
            "Run from an MSYS2 shell (MSYSTEM set) or disable windows.build_ffmpeg."
        )
    return [shell, configure.as_posix(), *args]


def _make_command(builder, make_args: list[str], env: dict[str, str]) -> list[str]:
    if builder.platform.os != "windows":
        return ["make", *make_args]
    if not windows_ffmpeg_native_build_enabled(builder):
        return ["make", *make_args]

    make = builder._resolve_windows_msys_tool(env, "make", "mingw32-make", "make.exe", "mingw32-make.exe")
    if not make:
        raise RuntimeError(
            "FFmpeg native build on Windows requires MSYS2 make in PATH. "
            "Run from an MSYS2 shell (MSYSTEM set) or disable windows.build_ffmpeg."
        )
    return [make, *make_args]


def _configured_prefix(build_dir: Path) -> str | None:
    for candidate in (build_dir / "ffbuild" / "config.mak", build_dir / "ffbuild" / "config.sh"):
        try:
            for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith("prefix="):
                    continue
                value = line.partition("=")[2].strip()
                if value:
                    return value
        except OSError:
            continue
    return None


def _prepare_build_dir(builder, ctx) -> None:
    if not builder.dry_run and ctx.build_dir.exists():
        shutil.rmtree(ctx.build_dir, ignore_errors=True)
    ctx.build_dir.mkdir(parents=True, exist_ok=True)


def _ensure_posix_line_endings(builder, src_dir: Path) -> None:
    if builder.platform.os == "windows" and not windows_ffmpeg_native_build_enabled(builder):
        return

    probe_paths = [
        src_dir / "configure",
        src_dir / "libavcodec" / "bitstream_filters.c",
        src_dir / "libavcodec" / "allcodecs.c",
        src_dir / "libavcodec" / "Makefile",
    ]
    files_with_cr: list[Path] = []
    checked = 0
    for probe in probe_paths:
        if not probe.exists():
            continue
        checked += 1
        if b"\r" in probe.read_bytes():
            files_with_cr.append(probe)

    if checked == 0 or not files_with_cr:
        return

    rel_paths = [str(path.relative_to(src_dir)) for path in files_with_cr]
    preview = ", ".join(rel_paths[:3])
    if len(rel_paths) > 3:
        preview += f", +{len(rel_paths) - 3} more"

    fix_cmds = [
        f"git -C {src_dir} config core.autocrlf false",
        f"git -C {src_dir} config core.eol lf",
        f"git -C {src_dir} reset --hard HEAD",
    ]
    fix_block = "\n".join(f"  {cmd}" for cmd in fix_cmds)
    raise RuntimeError(
        "FFmpeg checkout uses CRLF line endings on a POSIX host. "
        "This breaks configure (symptom: eval: ...\\r=yes: not found).\n"
        f"Detected in: {preview}\n"
        "Normalize line endings and retry:\n"
        f"{fix_block}"
    )


def install_only(builder, ctx, env: dict[str, str]) -> bool:
    if not (ctx.build_dir / "Makefile").exists():
        return False
    configure = ctx.src_dir / "configure"
    if not configure.exists():
        return False
    configured_prefix = _configured_prefix(ctx.build_dir)
    desired_prefix = os.path.normcase(os.path.normpath(str(ctx.install_prefix)))
    configured_prefix_norm = os.path.normcase(os.path.normpath(configured_prefix)) if configured_prefix else ""
    if configured_prefix_norm and configured_prefix_norm != desired_prefix:
        print(
            f"[note] {ctx.repo.name} ({ctx.build_type}) prefix changed from {configured_prefix} to {ctx.install_prefix}; reinstall requires rebuild",
            flush=True,
        )
        return False
    _ensure_posix_line_endings(builder, ctx.src_dir)
    ffmpeg_args = _configure_args(builder, ctx)
    cmd = _configure_command(builder, configure, ffmpeg_args, env)
    print_cmd("configure command", cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
    run(
        cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")),
    )

    install_cmd = _make_command(builder, ["install"], env)
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(
        install_cmd,
        cwd=str(ctx.build_dir),
        env=env,
        dry_run=builder.dry_run,
        log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")),
    )
    return True


def build(builder, ctx, env: dict[str, str]) -> None:
    _prepare_build_dir(builder, ctx)
    configure = ctx.src_dir / "configure"
    if not configure.exists():
        raise RuntimeError(f"Missing configure script for {ctx.repo.name}: {configure}")
    _ensure_posix_line_endings(builder, ctx.src_dir)
    ffmpeg_args = _configure_args(builder, ctx)
    cmd = _configure_command(builder, configure, ffmpeg_args, env)
    print_cmd("configure command", cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - configure")
    run(cmd, cwd=str(ctx.build_dir), env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "configure")))

    build_cmd = _make_command(builder, [f"-j{builder._jobs()}"], env)
    print_cmd("build command", build_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - building")
    run(build_cmd, cwd=str(ctx.build_dir), env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "build")))

    install_cmd = _make_command(builder, ["install"], env)
    print_cmd("install command", install_cmd)
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")
    run(install_cmd, cwd=str(ctx.build_dir), env=env, dry_run=builder.dry_run, log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "install")))
