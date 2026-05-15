from __future__ import annotations

from .policy import gl_enabled


def enabled(builder, _repo) -> bool:
    return gl_enabled(builder)


def patch_source(builder, src_dir) -> None:
    builder._patch_glew_macos(src_dir)
