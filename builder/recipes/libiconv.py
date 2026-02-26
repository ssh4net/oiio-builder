from __future__ import annotations


def enabled(builder, _repo) -> bool:
    return builder.platform.os == "windows"
