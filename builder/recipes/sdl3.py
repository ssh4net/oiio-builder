from __future__ import annotations

from pathlib import Path
import re


_FALSE_VALUES = {"0", "false", "off", "no", "n"}
_VIDEO_DRIVER_RE = re.compile(r"^#define SDL_VIDEO_DRIVER_(X11|WAYLAND) 1\s*$", re.MULTILINE)


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_sdl3)


def cmake_args(builder, _ctx) -> list[str]:
    # Build exactly the linkage selected by the prefix.
    static_linkage = bool(builder.config.global_cfg.static_default)
    args = [
        f"-DSDL_SHARED={'OFF' if static_linkage else 'ON'}",
        f"-DSDL_STATIC={'ON' if static_linkage else 'OFF'}",
    ]
    if builder.platform.os == "windows":
        # Upstream installs the MSVC package files directly in <prefix>/cmake;
        # keep them under lib/cmake/SDL3 like the other packages in the prefix.
        args.append("-DSDL_INSTALL_CMAKEDIR_ROOT=lib/cmake/SDL3")
    return args


def _cache_disabled(cache: dict, key: str) -> bool:
    value = cache.get(key)
    if value is None:
        return False
    if isinstance(value, bool):
        return not value
    return str(value).strip().lower() in _FALSE_VALUES


def linux_window_system_required(builder) -> bool:
    """False when the CMake overrides deliberately disable video or both X11 and Wayland."""
    cache = builder._repo_cmake_effective_toml_options("SDL3").cache
    return not (
        _cache_disabled(cache, "SDL_VIDEO")
        or (_cache_disabled(cache, "SDL_X11") and _cache_disabled(cache, "SDL_WAYLAND"))
    )


def post_install(builder, _install_prefix: Path, build_type: str) -> None:
    """Fail when a Linux build ended up without a window system.

    SDL3 only warns when the X11 and Wayland development packages are missing
    and then builds a library that can open no windows.
    """
    if builder.dry_run or builder.platform.os != "linux" or not linux_window_system_required(builder):
        return
    build_dir = builder.config.global_cfg.build_root / build_type / "SDL3"
    config_header = build_dir / f"include-config-{build_type.lower()}" / "build_config" / "SDL_build_config.h"
    try:
        text = config_header.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    if _VIDEO_DRIVER_RE.search(text):
        return
    raise RuntimeError(
        "SDL3: neither the X11 nor the Wayland video driver was enabled, so the library cannot open windows.\n"
        "Install the development packages (Debian/Ubuntu: sudo apt-get install libx11-dev libxext-dev "
        "libxcursor-dev libxi-dev libxfixes-dev libxrandr-dev libxss-dev libxtst-dev "
        "libwayland-dev libxkbcommon-dev libegl-dev wayland-protocols), "
        "then rebuild with: uv run build.py --force --only SDL3"
    )
