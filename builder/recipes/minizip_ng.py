from __future__ import annotations

from pathlib import Path

from .policy import ocio_enabled


STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return ocio_enabled(builder)


def cmake_args(builder, _ctx) -> list[str]:
    # minizip-ng defines MZ_EXPORTS for its shared target, but its public API
    # declarations do not use the MZ_EXPORT macro. Ask CMake to generate the
    # Windows export table so MSVC also produces the required import library.
    if builder.platform.os == "windows" and not builder.config.global_cfg.static_default:
        return ["-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=ON"]
    return []


def post_install(builder, install_prefix: Path, build_type: str) -> None:
    if builder.platform.os != "windows" or builder.config.global_cfg.static_default:
        return

    debug_postfix = str(builder.config.global_cfg.windows.get("debug_postfix", "d"))
    postfix = debug_postfix if build_type == "Debug" else ""
    import_library = install_prefix / "lib" / f"minizip-ng{postfix}.lib"
    if not import_library.is_file():
        raise RuntimeError(
            "minizip-ng shared Windows install is missing its import library: "
            f"{import_library}"
        )
