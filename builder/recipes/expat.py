from __future__ import annotations

STAMP_REVISION = "3"


def _without_utf8_flag(flags: str) -> str:
    return " ".join(flag for flag in flags.split() if flag.lower() != "/utf-8")


def cmake_args(builder, ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []

    # Expat's CMakeLists explicitly supplies /source-charset:utf-8. Current
    # MSVC rejects that together with the builder's broader /utf-8 switch
    # (D8016), so override only this C project's initial C flags. Recipe CMake
    # arguments are appended after generic flags and therefore win.
    cflags = _without_utf8_flag(builder._base_flags(ctx.build_type))
    args = [f"-DCMAKE_C_FLAGS_INIT={cflags}"]

    runtime_mode = str(builder.config.global_cfg.windows.get("msvc_runtime", "static")).strip().lower()
    if runtime_mode in {"", "static", "mt", "multithreaded"}:
        args.append("-DEXPAT_MSVC_STATIC_CRT=ON")
    elif runtime_mode in {"dynamic", "md", "multithreadeddll"}:
        args.append("-DEXPAT_MSVC_STATIC_CRT=OFF")
    return args
