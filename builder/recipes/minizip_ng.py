from __future__ import annotations

from .policy import ocio_enabled


def enabled(builder, _repo) -> bool:
    return ocio_enabled(builder)
