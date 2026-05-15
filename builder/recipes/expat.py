from __future__ import annotations


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []

    runtime_mode = str(builder.config.global_cfg.windows.get("msvc_runtime", "static")).strip().lower()
    if runtime_mode in {"", "static", "mt", "multithreaded"}:
        return ["-DEXPAT_MSVC_STATIC_CRT=ON"]
    if runtime_mode in {"dynamic", "md", "multithreadeddll"}:
        return ["-DEXPAT_MSVC_STATIC_CRT=OFF"]
    return []
