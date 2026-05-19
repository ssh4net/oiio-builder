from __future__ import annotations

import shutil

from .policy import cpython_requested
from ..runner import banner
from ..vcpkg_import import add_debug_postfix, copy_bin_payload, find_triplet, resolve_export_zip, stage_export


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return cpython_requested(builder)


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os == "windows":
        return "sqlite"
    return None


def _zip_path(builder, env=None):
    return resolve_export_zip(
        builder,
        env,
        default_filename="vcpkg-export-sqlite.zip",
        env_names=(
            "SQLITE_VCPKG_EXPORT_ZIP",
            "VCPKG_SQLITE_EXPORT_ZIP",
            "SQLITE3_VCPKG_EXPORT_ZIP",
            "VCPKG_SQLITE3_EXPORT_ZIP",
        ),
        glob_pattern="vcpkg-export-sqlite*.zip",
    )


def missing_source_skip(builder, repo, _path) -> bool | None:
    if builder.platform.os != "windows":
        return None
    zip_path = _zip_path(builder)
    if zip_path.exists():
        return False
    if repo.optional:
        print(f"[skip] {repo.name}: missing vcpkg export zip at {zip_path}")
        return True
    return False


def skip_update(builder, _repo) -> bool:
    return builder.platform.os == "windows"


def stamp_payload(builder, _repo, _ctx, payload: dict) -> None:
    if builder.platform.os != "windows":
        return
    zip_path = _zip_path(builder)
    payload["vcpkg_export_zip"] = str(zip_path)
    if zip_path.exists():
        st = zip_path.stat()
        payload["vcpkg_export_zip_size"] = int(st.st_size)
        payload["vcpkg_export_zip_mtime"] = int(st.st_mtime)


def build(builder, ctx, env: dict[str, str]) -> None:
    if builder.platform.os != "windows":
        raise RuntimeError("sqlite build system is only supported on Windows")

    zip_path = _zip_path(builder, env)
    installed_dir = stage_export(builder, ctx, env, zip_path, "_sqlite_vcpkg_export")
    if installed_dir is None:
        return

    triplet_dir = find_triplet(installed_dir, "include/sqlite3.h", zip_path)
    include_src = triplet_dir / "include"
    lib_src = triplet_dir / "lib"
    debug_lib_src = triplet_dir / "debug" / "lib"
    bin_src = triplet_dir / "bin"

    sqlite_release = lib_src / "sqlite3.lib"
    sqlite_debug = debug_lib_src / "sqlite3.lib"
    required = [include_src / "sqlite3.h", sqlite_release, sqlite_debug]
    missing = [p for p in required if not p.exists()]
    if missing:
        wanted = "\n".join(f"  - {p}" for p in missing)
        raise RuntimeError(f"sqlite vcpkg export is missing expected files:\n{wanted}")

    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    banner(f"{ctx.repo.name} ({ctx.build_type}) - install")

    inc_dst = ctx.install_prefix / "include"
    lib_dst = ctx.install_prefix / "lib"
    bin_dst = ctx.install_prefix / "bin"
    inc_dst.mkdir(parents=True, exist_ok=True)
    lib_dst.mkdir(parents=True, exist_ok=True)

    for item in include_src.iterdir():
        if item.is_file() and item.name.lower().startswith("sqlite3"):
            shutil.copy2(item, inc_dst / item.name)

    shutil.copy2(sqlite_release, lib_dst / sqlite_release.name)
    shutil.copy2(sqlite_debug, lib_dst / add_debug_postfix(sqlite_release.name, debug_postfix))
    copy_bin_payload(bin_src, bin_dst, "sqlite")
