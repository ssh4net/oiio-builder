from __future__ import annotations


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._ensure_libdeflate_alias(install_prefix, build_type)
