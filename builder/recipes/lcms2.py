from __future__ import annotations

STAMP_REVISION = "2"


def cmake_args(_builder, _ctx) -> list[str]:
    return [
        "-DBUILD_TESTING=OFF",
        "-DBUILD_TESTS=OFF",
        "-DLCMS2_WITH_TIFF=OFF",
        "-DLCMS2_BUILD_TIFICC=OFF",
    ]
