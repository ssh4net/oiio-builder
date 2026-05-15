from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._ensure_png16_include_alias(ctx.install_prefix)


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_png16_include_alias(install_prefix)
