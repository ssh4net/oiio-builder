from __future__ import annotations


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._ensure_unofficial_brotli_package(install_prefix, build_type)
