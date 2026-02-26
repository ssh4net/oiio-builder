from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_pybind11)
