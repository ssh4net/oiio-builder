from __future__ import annotations

from .policy import exr_enabled


def enabled(builder, _repo) -> bool:
    return exr_enabled(builder)
