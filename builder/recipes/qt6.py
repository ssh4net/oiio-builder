from __future__ import annotations

from .policy import qt6_enabled


def enabled(builder, _repo) -> bool:
    return qt6_enabled(builder)


def patch_source(builder, src_dir) -> None:
    builder._prepare_qt6_sources(src_dir)
