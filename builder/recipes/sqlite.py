from __future__ import annotations

from .policy import cpython_requested


def enabled(builder, _repo) -> bool:
    return cpython_requested(builder)

