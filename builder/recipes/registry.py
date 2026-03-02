from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

from . import (
    aom,
    brotli,
    bzip2,
    cpython,
    dng_sdk,
    eigen,
    ffmpeg,
    fmt,
    freetype,
    freeglut,
    giflib,
    glfw,
    glew,
    googletest,
    harfbuzz,
    imath,
    jasper,
    kvazaar,
    lcms2,
    lbfgspp,
    libiconv,
    libdeflate,
    libjpeg_turbo,
    libraw_legacy,
    libraw,
    libde265,
    libheif,
    libjxl,
    libpng,
    libtiff,
    libultrahdr,
    libwebp,
    minizip_ng,
    nanobind,
    opencolorio,
    openssl,
    openimageio,
    openexr,
    openjpeg,
    openjph,
    pcre2,
    ptex,
    pugixml,
    pybind11,
    pystring,
    qt6,
    robinmap,
    spdlog,
    x265,
)

_RECIPES: dict[str, ModuleType] = {
    "aom": aom,
    "brotli": brotli,
    "bzip2": bzip2,
    "cpython": cpython,
    "dng-sdk": dng_sdk,
    "eigen": eigen,
    "ffmpeg": ffmpeg,
    "fmt": fmt,
    "freetype": freetype,
    "freeglut": freeglut,
    "giflib": giflib,
    "glfw": glfw,
    "glew": glew,
    "googletest": googletest,
    "harfbuzz": harfbuzz,
    "imath": imath,
    "jasper": jasper,
    "kvazaar": kvazaar,
    "LBFGSpp": lbfgspp,
    "lcms2": lcms2,
    "libdeflate": libdeflate,
    "libiconv": libiconv,
    "libjpeg-turbo": libjpeg_turbo,
    "LibRaw": libraw_legacy,
    "libraw": libraw,
    "libde265": libde265,
    "libheif": libheif,
    "libjxl": libjxl,
    "libpng": libpng,
    "libtiff": libtiff,
    "libultrahdr": libultrahdr,
    "libwebp": libwebp,
    "minizip-ng": minizip_ng,
    "nanobind": nanobind,
    "OpenColorIO": opencolorio,
    "openssl": openssl,
    "OpenImageIO": openimageio,
    "openexr": openexr,
    "openjpeg": openjpeg,
    "openjph": openjph,
    "pcre2": pcre2,
    "ptex": ptex,
    "pugixml": pugixml,
    "pybind11": pybind11,
    "pystring": pystring,
    "Qt6": qt6,
    "robinmap": robinmap,
    "spdlog": spdlog,
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


def enabled(repo_name: str, builder: Any, repo: Any) -> bool | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "enabled", None)
    if not callable(func):
        return None
    return bool(func(builder, repo))


def post_install(repo_name: str, builder: Any, install_prefix: Path, build_type: str) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "post_install", None)
    if callable(func):
        func(builder, install_prefix, build_type)


def stamp_revision(repo_name: str) -> str | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    revision = getattr(recipe, "STAMP_REVISION", None)
    if revision is None:
        return None
    return str(revision)
