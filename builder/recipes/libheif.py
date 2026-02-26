from __future__ import annotations

from pathlib import Path

STAMP_REVISION = "1"

from .policy import imageio_enabled


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    return imageio_enabled(builder) and bool(cfg.build_libheif)


def cmake_args(builder, ctx) -> list[str]:
    if builder.platform.os != "windows":
        return []

    cfg = builder.config.global_cfg
    debug_postfix = str(cfg.windows.get("debug_postfix", "d"))
    lib_dir = (ctx.install_prefix / "lib").resolve()
    include_dir = (ctx.install_prefix / "include").resolve()

    def _pick_lib(base: str) -> Path | None:
        if ctx.build_type == "Debug":
            preferred = [lib_dir / f"{base}{debug_postfix}.lib", lib_dir / f"{base}.lib"]
        else:
            preferred = [lib_dir / f"{base}.lib", lib_dir / f"{base}{debug_postfix}.lib"]
        for candidate in preferred:
            if candidate.exists():
                return candidate
        matches = sorted(lib_dir.glob(f"{base}*.lib"))
        return matches[0] if matches else None

    args: list[str] = []
    x265_lib = _pick_lib("x265-static")
    if x265_lib is not None and (include_dir / "x265.h").exists():
        args += [
            f"-DX265_INCLUDE_DIR={include_dir}",
            f"-DX265_LIBRARY={x265_lib}",
        ]

    libde265_lib = _pick_lib("libde265")
    if libde265_lib is not None and (include_dir / "libde265" / "de265.h").exists():
        args += [
            f"-DLIBDE265_INCLUDE_DIR={include_dir}",
            f"-DLIBDE265_LIBRARY={libde265_lib}",
        ]

    kvazaar_lib = _pick_lib("libkvazaar")
    if kvazaar_lib is not None and (include_dir / "kvazaar.h").exists():
        args += [
            f"-DKVAZAAR_INCLUDE_DIR={include_dir}",
            f"-DKVAZAAR_LIBRARY={kvazaar_lib}",
        ]

    return args


def patch_source(_builder, src_dir: Path) -> None:
    cmake_file = src_dir / "libheif" / "plugins" / "CMakeLists.txt"
    if not cmake_file.exists():
        return

    original_text = cmake_file.read_text(encoding="utf-8")
    text = original_text

    marker = "# oiio-builder: windows static deps"
    if marker not in text:
        needle = "target_link_libraries(heif PRIVATE ${${varName}_LIBRARIES})"
        insert = (
            f"{needle}\n"
            f"            {marker}\n"
            "            if (WIN32)\n"
            "                if (\"${optionName}\" STREQUAL \"LIBDE265\")\n"
            "                    target_compile_definitions(heif PRIVATE LIBDE265_STATIC_BUILD)\n"
            "                elseif (\"${optionName}\" STREQUAL \"KVAZAAR\")\n"
            "                    target_compile_definitions(heif PRIVATE KVZ_STATIC_LIB)\n"
            "                endif ()\n"
            "            endif ()"
        )
        if needle in text:
            text = text.replace(needle, insert, 1)

    if text != original_text:
        cmake_file.write_text(text, encoding="utf-8")


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_libheif_aom_dependency(install_prefix)
    builder._ensure_libheif_consumer_definitions(install_prefix)
