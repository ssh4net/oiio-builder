from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from . import lcms2, libjxl, libtiff, openexr, openjpeg

_RECIPES: dict[str, ModuleType] = {
    "lcms2": lcms2,
    "libjxl": libjxl,
    "libtiff": libtiff,
    "openexr": openexr,
    "openjpeg": openjpeg,
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
