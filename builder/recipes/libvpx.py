from __future__ import annotations


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_libvpx)


def resolve_build_system(builder, _repo, _src_dir) -> str | None:
    if builder.platform.os == "windows":
        return "libvpx"
    return None


def _zip_path(builder, env=None):
    return builder._libvpx_export_zip(env)


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


def autotools_args(builder, _repo) -> list[str]:
    args = [
        "--disable-examples",
        "--disable-tools",
        "--disable-docs",
        "--disable-unit-tests",
    ]
    if builder.config.global_cfg.pic:
        args.append("--enable-pic")
    return args
