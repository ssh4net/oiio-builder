from __future__ import annotations

from .policy import qt6_enabled


def enabled(builder, _repo) -> bool:
    return qt6_enabled(builder)


def patch_source(builder, src_dir) -> None:
    builder._prepare_qt6_sources(src_dir)


def stamp_payload(builder, _repo, ctx, payload: dict) -> None:
    qt_submodules = builder._qt6_submodules()
    qt_submodule_set = set(qt_submodules)
    system_libs = {
        "pcre": "system",
        "zlib": "system",
        "freetype": "system",
        "harfbuzz": "system",
        "libpng": "system",
        "libjpeg": "system",
    }
    if "qtimageformats" in qt_submodule_set:
        system_libs["tiff"] = "system"
        system_libs["webp"] = "system"
    disabled_features = ["gstreamer", "pipewire"] if "qtmultimedia" in qt_submodule_set else []
    if builder.platform.os == "linux":
        disabled_features.extend(["dbus", "glib"])
    payload["qt6"] = {
        "submodules": qt_submodules,
        "mode": "debug" if ctx.build_type == "Debug" else "release",
        "opengl": "desktop" if builder.platform.os in {"linux", "macos"} else "default",
        "qpa": (
            "xcb;wayland"
            if builder.platform.os == "linux" and "qtwayland" in qt_submodule_set
            else ("xcb" if builder.platform.os == "linux" else "default")
        ),
        "qpa_default": ("xcb" if builder.platform.os == "linux" else "default"),
        "ssl": ("openssl-linked" if builder.platform.os in {"linux", "windows"} else "default"),
        "static_runtime": (builder.platform.os == "windows"),
        "system_libs": system_libs,
        "disabled_features": sorted(disabled_features),
        "feature_ffmpeg": (
            builder.platform.os != "windows" and "qtmultimedia" in qt_submodule_set and builder._ffmpeg_enabled()
        ),
        "pkg_config_use_static_libs": True,
    }
