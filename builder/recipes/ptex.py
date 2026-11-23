from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return bool(cfg.build_ptex) and (imageio_enabled(builder) or cfg.build_opensubdiv)
