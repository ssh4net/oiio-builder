from __future__ import annotations

from .policy import qt6_enabled


def enabled(builder, _repo) -> bool:
    return builder.platform.os == "windows" and qt6_enabled(builder)
