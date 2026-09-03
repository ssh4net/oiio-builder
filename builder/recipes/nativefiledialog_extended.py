from __future__ import annotations

import os
from pathlib import Path


STAMP_REVISION = "1"


def _patch_wayland_protocol_fallback(src_dir: Path) -> None:
    cmake_path = src_dir / "src" / "CMakeLists.txt"
    if not cmake_path.exists():
        raise RuntimeError(f"nativefiledialog-extended CMake patch target is missing: {cmake_path}")

    text = cmake_path.read_text(encoding="utf-8", errors="replace")
    marker = "OIIO_BUILDER_WAYLAND_PROTOCOLS_FALLBACK"
    if marker in text:
        return

    old = """\
    set(NFD_WAYLAND_PROTOCOL_XDG_FOREIGN ${CMAKE_CURRENT_SOURCE_DIR}/../3ps/wayland-protocols/unstable/xdg-foreign/xdg-foreign-unstable-v1.xml)
"""
    new = """\
    set(NFD_WAYLAND_PROTOCOL_XDG_FOREIGN ${CMAKE_CURRENT_SOURCE_DIR}/../3ps/wayland-protocols/unstable/xdg-foreign/xdg-foreign-unstable-v1.xml)
    # OIIO_BUILDER_WAYLAND_PROTOCOLS_FALLBACK
    if(NOT EXISTS "${NFD_WAYLAND_PROTOCOL_XDG_FOREIGN}")
      pkg_get_variable(NFD_WAYLAND_PROTOCOLS_PKGDATADIR wayland-protocols pkgdatadir)
      if(NFD_WAYLAND_PROTOCOLS_PKGDATADIR)
        set(NFD_WAYLAND_PROTOCOL_XDG_FOREIGN "${NFD_WAYLAND_PROTOCOLS_PKGDATADIR}/unstable/xdg-foreign/xdg-foreign-unstable-v1.xml")
      endif()
    endif()
    if(NOT EXISTS "${NFD_WAYLAND_PROTOCOL_XDG_FOREIGN}")
      message(FATAL_ERROR "Wayland protocol xdg-foreign XML not found. Initialize the nativefiledialog-extended 3ps/wayland-protocols submodule, install wayland-protocols, or configure with -DNFD_WAYLAND=OFF.")
    endif()
"""
    if old not in text:
        raise RuntimeError(f"nativefiledialog-extended Wayland protocol patch no longer matches upstream source: {cmake_path}")

    cmake_path.write_text(text.replace(old, new, 1), encoding="utf-8")


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os != "linux":
        return []
    # Keep system GTK/dbus pkg-config resolution isolated from the builder prefix.
    return ["-DPKG_CONFIG_USE_CMAKE_PREFIX_PATH=FALSE"]


def patch_source(builder, src_dir: Path) -> None:
    if builder.platform.os == "linux" and not builder.dry_run:
        _patch_wayland_protocol_fallback(src_dir)


def build_env(builder, _repo, build_type: str, prefix, env: dict[str, str]) -> None:
    if builder.platform.os != "linux":
        return

    override_dir = builder.pkg_override_root / build_type
    remove_norm = {
        os.path.normcase(os.path.normpath(str(override_dir))),
        os.path.normcase(os.path.normpath(str(prefix / "lib" / "pkgconfig"))),
        os.path.normcase(os.path.normpath(str(prefix / "share" / "pkgconfig"))),
    }
    current = env.get("PKG_CONFIG_PATH", "")
    items = current.split(os.pathsep) if current else []
    kept: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        normalized = os.path.normcase(os.path.normpath(item))
        if normalized in remove_norm:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        kept.append(item)
    if kept:
        env["PKG_CONFIG_PATH"] = os.pathsep.join(kept)
    else:
        env.pop("PKG_CONFIG_PATH", None)
