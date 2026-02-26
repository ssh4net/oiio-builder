from __future__ import annotations


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return bool(cfg.build_gtest)
