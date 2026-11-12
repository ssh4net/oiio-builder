from __future__ import annotations

from .policy import imageio_enabled


STAMP_REVISION = "1"


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os == "linux":
        # The builder configures before every rebuild. Avoid Ninja glob-check
        # regeneration loops on shared source/build filesystems.
        return ["-DCMAKE_SUPPRESS_REGENERATION=ON"]
    return []


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_pybind11)
