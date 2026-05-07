from __future__ import annotations

from .policy import cpython_requested


STAMP_REVISION = "3"


def enabled(builder, _repo) -> bool:
    return cpython_requested(builder)
