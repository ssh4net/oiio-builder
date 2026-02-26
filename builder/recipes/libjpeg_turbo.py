from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)
