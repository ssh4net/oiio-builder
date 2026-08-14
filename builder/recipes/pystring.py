from __future__ import annotations

STAMP_REVISION = "1"


def cmake_args(builder, _ctx) -> list[str]:
    # pystring has no dllexport annotations. For a Windows shared build, ask
    # CMake to export its symbols so MSVC also creates the import library.
    if builder.platform.os == "windows" and not builder.config.global_cfg.static_default:
        return ["-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=ON"]
    return []


def post_install(builder, install_prefix, build_type: str) -> None:
    builder._ensure_pystring_package(install_prefix, build_type)
