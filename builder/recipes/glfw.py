from __future__ import annotations

from .policy import gl_enabled


def enabled(builder, _repo) -> bool:
    return gl_enabled(builder) or bool(getattr(builder.config.global_cfg, "build_geogram", False))
