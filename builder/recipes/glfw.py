from __future__ import annotations

from .policy import gl_enabled


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return gl_enabled(builder) or bool(cfg.build_geogram or cfg.build_opensubdiv)
