from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    # Legacy libraw source tree fallback repo.
    return imageio_enabled(builder)
