from __future__ import annotations


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_toml11)
