from __future__ import annotations

from .policy import cpython_requested


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return cpython_requested(builder)


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os == "windows":
        return "sqlite"
    return None


def _zip_path(builder, env=None):
    return builder._sqlite_export_zip(env)


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
