from __future__ import annotations

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._ensure_bzip2_alias(install_prefix, build_type)
    builder._ensure_bzip2_package(install_prefix, build_type)
