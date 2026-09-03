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
    expat,
    ffmpeg,
    fmt,
    freetype,
    freeglut,
    giflib,
    glfw,
    glslang,
    glew,
    googletest,
    harfbuzz,
    highway,
    imgui,
    imgui_test_engine,
    imath,
    jasper,
    kvazaar,
    lcms2,
    lbfgspp,
    libdeflate,
    libiconv,
    libjpeg_turbo,
    libxml2,
    libraw_legacy,
    libraw,
    libsodium,
    libde265,
    libheif,
    libjxl,
    libpng,
    libtiff,
    libultrahdr,
    libwebp,
    libvpx,
    libyuv,
    minizip_ng,
    miniply,
    nanobind,
    nativefiledialog_extended,
    nlohmann_json,
    opencolorio,
    openssl,
    openmeta,
    openimageio,
    openexr,
    openjpeg,
    openjph,
    opus,
    pcre2,
    ptex,
    pugixml,
    pybind11,
    pystring,
    qt6,
    rapidfuzz_cpp,
    rapidobj,
    robinmap,
    sqlite,
    spdlog,
    spirv_headers,
    spirv_tools,
    toml11,
    x265,
    xz,
    yaml_cpp,
    zlib_ng,
    zstd,
)

_RECIPES: dict[str, ModuleType] = {
    "aom": aom,
    "brotli": brotli,
    "bzip2": bzip2,
    "cpython": cpython,
    "dng-sdk": dng_sdk,
    "eigen": eigen,
    "expat": expat,
    "ffmpeg": ffmpeg,
    "fmt": fmt,
    "freetype": freetype,
    "freeglut": freeglut,
    "giflib": giflib,
    "glfw": glfw,
    "glslang": glslang,
    "glew": glew,
    "googletest": googletest,
    "harfbuzz": harfbuzz,
    "highway": highway,
    "imgui": imgui,
    "imgui_test_engine": imgui_test_engine,
    "imath": imath,
    "jasper": jasper,
    "kvazaar": kvazaar,
    "LBFGSpp": lbfgspp,
    "lcms2": lcms2,
    "libdeflate": libdeflate,
    "libiconv": libiconv,
    "libjpeg-turbo": libjpeg_turbo,
    "libxml2": libxml2,
    "LibRaw": libraw_legacy,
    "libraw": libraw,
    "libsodium": libsodium,
    "libde265": libde265,
    "libheif": libheif,
    "libjxl": libjxl,
    "libpng": libpng,
    "libtiff": libtiff,
    "libultrahdr": libultrahdr,
    "libwebp": libwebp,
    "libvpx": libvpx,
    "libyuv": libyuv,
    "minizip-ng": minizip_ng,
    "miniply": miniply,
    "nanobind": nanobind,
    "nativefiledialog-extended": nativefiledialog_extended,
    "nlohmann-json": nlohmann_json,
    "OpenColorIO": opencolorio,
    "openssl": openssl,
    "OpenMeta": openmeta,
    "OpenImageIO": openimageio,
    "openexr": openexr,
    "openjpeg": openjpeg,
    "openjph": openjph,
    "opus": opus,
    "pcre2": pcre2,
    "ptex": ptex,
    "pugixml": pugixml,
    "pybind11": pybind11,
    "pystring": pystring,
    "Qt6": qt6,
    "rapidfuzz-cpp": rapidfuzz_cpp,
    "rapidobj": rapidobj,
    "robinmap": robinmap,
    "sqlite": sqlite,
    "spdlog": spdlog,
    "SPIRV-Headers": spirv_headers,
    "SPIRV-Tools": spirv_tools,
    "toml11": toml11,
    "x265": x265,
    "xz": xz,
    "yaml-cpp": yaml_cpp,
    "zlib-ng": zlib_ng,
    "zstd": zstd,
}


def cmake_args(repo_name: str, builder: Any, ctx: Any) -> list[str] | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "cmake_args", None)
    if not callable(func):
        return None
    return list(func(builder, ctx))


def build_env(repo_name: str, builder: Any, repo: Any, build_type: str, prefix: Path, env: dict[str, str]) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "build_env", None)
    if callable(func):
        func(builder, repo, build_type, prefix, env)


def autotools_args(repo_name: str, builder: Any, repo: Any) -> list[str] | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "autotools_args", None)
    if not callable(func):
        return None
    return list(func(builder, repo))


def patch_source(repo_name: str, builder: Any, src_dir: Path) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "patch_source", None)
    if callable(func):
        func(builder, src_dir)


def pre_build(repo_name: str, builder: Any, repo: Any, ctx: Any, env: dict[str, str]) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "pre_build", None)
    if callable(func):
        func(builder, repo, ctx, env)


def build_backend(repo_name: str, builder: Any, ctx: Any, env: dict[str, str]) -> bool:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return False
    func = getattr(recipe, "build", None)
    if not callable(func):
        return False
    func(builder, ctx, env)
    return True


def install_only(repo_name: str, builder: Any, ctx: Any, env: dict[str, str]) -> bool | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "install_only", None)
    if not callable(func):
        return None
    return bool(func(builder, ctx, env))


def enabled(repo_name: str, builder: Any, repo: Any) -> bool | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "enabled", None)
    if not callable(func):
        return None
    return bool(func(builder, repo))


def resolve_build_system(repo_name: str, builder: Any, repo: Any, src_dir: Path) -> str | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "resolve_build_system", None)
    if not callable(func):
        return None
    result = func(builder, repo, src_dir)
    return None if result is None else str(result)


def missing_source_skip(repo_name: str, builder: Any, repo: Any, path: Path) -> bool | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    func = getattr(recipe, "missing_source_skip", None)
    if not callable(func):
        return None
    result = func(builder, repo, path)
    return None if result is None else bool(result)


def skip_update(repo_name: str, builder: Any, repo: Any) -> bool:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return False
    func = getattr(recipe, "skip_update", None)
    if not callable(func):
        return False
    return bool(func(builder, repo))


def post_install(repo_name: str, builder: Any, install_prefix: Path, build_type: str) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "post_install", None)
    if callable(func):
        func(builder, install_prefix, build_type)


def stamp_payload(repo_name: str, builder: Any, repo: Any, ctx: Any, payload: dict[str, Any]) -> None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return
    func = getattr(recipe, "stamp_payload", None)
    if callable(func):
        func(builder, repo, ctx, payload)


def stamp_revision(repo_name: str) -> str | None:
    recipe = _RECIPES.get(repo_name)
    if recipe is None:
        return None
    revision = getattr(recipe, "STAMP_REVISION", None)
    if revision is None:
        return None
    return str(revision)
