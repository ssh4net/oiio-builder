from __future__ import annotations

import os


def cmake_args(builder, _ctx) -> list[str]:
    if builder.platform.os != "linux":
        return []
    # Keep system GTK/dbus pkg-config resolution isolated from the builder prefix.
    return ["-DPKG_CONFIG_USE_CMAKE_PREFIX_PATH=FALSE"]


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
