from __future__ import annotations

STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_opus)


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []

    if builder._windows_runtime_mode() == "static":
        return ["-DOPUS_STATIC_RUNTIME=ON"]
    return ["-DOPUS_STATIC_RUNTIME=OFF"]
