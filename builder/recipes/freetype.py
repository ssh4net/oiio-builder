from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def cmake_args(builder, ctx) -> list[str]:
    cfg = builder.config.global_cfg
    bz_include = (ctx.install_prefix / "include").resolve()
    lib_dir = (ctx.install_prefix / "lib").resolve()
    bzip2_release = None
    bzip2_debug = None
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        builder._ensure_bzip2_alias(ctx.install_prefix, ctx.build_type)
        bzip2_release_candidates = [
            lib_dir / "bz2_static.lib",
            lib_dir / "bz2.lib",
            lib_dir / "libbz2_static.lib",
            lib_dir / "libbz2.lib",
        ]
        bzip2_debug_candidates = [
            lib_dir / f"bz2_static{debug_postfix}.lib",
            lib_dir / f"bz2{debug_postfix}.lib",
            lib_dir / f"libbz2_static{debug_postfix}.lib",
            lib_dir / f"libbz2{debug_postfix}.lib",
            lib_dir / "bz2_static.lib",
            lib_dir / "bz2.lib",
        ]
        bzip2_release = next((candidate for candidate in bzip2_release_candidates if candidate.exists()), None)
        bzip2_debug = next((candidate for candidate in bzip2_debug_candidates if candidate.exists()), None)
        if bzip2_release is None:
            matches = sorted(lib_dir.glob("*bz2*.lib"))
            if matches:
                bzip2_release = matches[0]
        if bzip2_debug is None:
            matches = sorted(lib_dir.glob(f"*bz2*{debug_postfix}*.lib"))
            if matches:
                bzip2_debug = matches[0]
            elif bzip2_release is not None:
                bzip2_debug = bzip2_release
    else:
        bzip2_release_candidates = [
            lib_dir / "libbz2_static.a",
            lib_dir / "libbz2.a",
            lib_dir / "libbz2.so",
            lib_dir / "libbz2.dylib",
        ]
        bzip2_debug_candidates = [
            lib_dir / "libbz2_staticd.a",
            lib_dir / "libbz2d.a",
            lib_dir / "libbz2_static.a",
            lib_dir / "libbz2.a",
        ]
        bzip2_release = next((candidate for candidate in bzip2_release_candidates if candidate.exists()), None)
        bzip2_debug = next((candidate for candidate in bzip2_debug_candidates if candidate.exists()), None)
        if bzip2_debug is None and bzip2_release is not None:
            bzip2_debug = bzip2_release

    args: list[str] = []
    if (bz_include / "bzlib.h").exists():
        args.append(f"-DBZIP2_INCLUDE_DIR={bz_include.as_posix()}")
    if bzip2_release is not None:
        args.append(f"-DBZIP2_LIBRARY_RELEASE={bzip2_release.as_posix()}")
    if bzip2_debug is not None:
        args.append(f"-DBZIP2_LIBRARY_DEBUG={bzip2_debug.as_posix()}")
    bzip2_default = bzip2_debug if ctx.build_type == "Debug" else bzip2_release
    if bzip2_default is not None:
        args.append(f"-DBZIP2_LIBRARY={bzip2_default.as_posix()}")
        args.append(f"-DBZIP2_LIBRARIES={bzip2_default.as_posix()}")
    if builder.platform.os == "windows":
        debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
        hb_include_candidates = [
            (ctx.install_prefix / "include" / "harfbuzz").resolve(),
            (ctx.install_prefix / "include").resolve(),
        ]
        hb_include_dir = next((candidate for candidate in hb_include_candidates if (candidate / "hb.h").exists()), None)

        if ctx.build_type == "Debug":
            hb_lib_candidates = [
                lib_dir / f"harfbuzz{debug_postfix}.lib",
                lib_dir / f"libharfbuzz{debug_postfix}.lib",
                lib_dir / "harfbuzz.lib",
                lib_dir / "libharfbuzz.lib",
            ]
        else:
            hb_lib_candidates = [
                lib_dir / "harfbuzz.lib",
                lib_dir / "libharfbuzz.lib",
                lib_dir / f"harfbuzz{debug_postfix}.lib",
                lib_dir / f"libharfbuzz{debug_postfix}.lib",
            ]
        hb_library = next((candidate for candidate in hb_lib_candidates if candidate.exists()), None)
        if hb_library is None:
            matches = sorted(lib_dir.glob("*harfbuzz*.lib"))
            if matches:
                hb_library = matches[0]

        if hb_include_dir is not None:
            args.append(f"-DHarfBuzz_INCLUDE_DIR={hb_include_dir.as_posix()}")
        if hb_library is not None:
            args.append(f"-DHarfBuzz_LIBRARY={hb_library.as_posix()}")
    return args


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._ensure_freetype_harfbuzz_compat(install_prefix, build_type)
