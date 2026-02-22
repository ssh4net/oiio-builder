from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from . import (
    dng_sdk,
    kvazaar,
    lcms2,
    libraw,
    libde265,
    libheif,
    libjxl,
    libtiff,
    libultrahdr,
    opencolorio,
    openimageio,
    openexr,
    openjpeg,
    openjph,
    x265,
)

_RECIPES: dict[str, ModuleType] = {
    "dng-sdk": dng_sdk,
    "kvazaar": kvazaar,
    "lcms2": lcms2,
    "libraw": libraw,
    "libde265": libde265,
    "libheif": libheif,
    "libjxl": libjxl,
    "libtiff": libtiff,
    "libultrahdr": libultrahdr,
    "OpenColorIO": opencolorio,
    "OpenImageIO": openimageio,
    "openexr": openexr,
    "openjpeg": openjpeg,
    "openjph": openjph,
    "x265": x265,
}


def cmake_args(repo_name: str, builder: Any, ctx: Any) -> list[str] | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "cmake_args", None)
    if not callable(func):
        return None
    return list(func(builder, ctx))


def patch_source(repo_name: str, builder: Any, src_dir: Path) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "patch_source", None)
    if callable(func):
        func(builder, src_dir)


def stamp_revision(repo_name: str) -> str | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    revision = getattr(recipe, "STAMP_REVISION", None)
    if revision is None:
        return None
    return str(revision)
