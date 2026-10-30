from __future__ import annotations

from pathlib import Path

from .policy import imageio_enabled


STAMP_REVISION = "2"


def enabled(builder, _repo) -> bool:
    return imageio_enabled(builder)


def patch_source(builder, src_dir: Path) -> None:
    cmake_lists = src_dir / "CMakeLists.txt"
    if getattr(builder, "dry_run", False) or not cmake_lists.is_file():
        return
    original = cmake_lists.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    updated = text

    # These aliases are ordinary files. Shared-library and framework links
    # retain upstream's link semantics; CIFS needs copies for static builds.
    for alias, source in (
        ("libpng.pc", "libpng${PNGLIB_ABI_VERSION}.pc"),
        ("libpng-config", "libpng${PNGLIB_ABI_VERSION}-config"),
    ):
        updated = updated.replace(
            f"create_symlink({alias} FILE {source})",
            f'file(REMOVE "${{CMAKE_CURRENT_BINARY_DIR}}/{alias}")\n'
            f'  configure_file("${{CMAKE_CURRENT_BINARY_DIR}}/{source}"\n'
            f'                 "${{CMAKE_CURRENT_BINARY_DIR}}/{alias}" COPYONLY)',
        )
    updated = updated.replace(
        "create_symlink(libpng${CMAKE_STATIC_LIBRARY_SUFFIX} TARGET png_static)",
        "add_custom_command(TARGET png_static POST_BUILD\n"
        '                         COMMAND "${CMAKE_COMMAND}" -E remove -f\n'
        '                                 "$<TARGET_LINKER_FILE_DIR:png_static>/libpng${CMAKE_STATIC_LIBRARY_SUFFIX}"\n'
        '                         COMMAND "${CMAKE_COMMAND}" -E copy_if_different\n'
        '                                 "$<TARGET_LINKER_FILE:png_static>"\n'
        '                                 "$<TARGET_LINKER_FILE_DIR:png_static>/libpng${CMAKE_STATIC_LIBRARY_SUFFIX}"\n'
        "                         VERBATIM)",
    )
    if updated != text:
        cmake_lists.write_bytes(updated.replace("\n", newline).encode("utf-8"))


def _posix_line_ending_files(src_dir: Path) -> list[Path]:
    paths = [
        src_dir / "png.h",
        src_dir / "pngconf.h",
        src_dir / "pnglibconf.h.prebuilt",
        src_dir / "pngusr.dfa",
        src_dir / "libpng-config.in",
        src_dir / "libpng.pc.in",
        src_dir / "scripts" / "macro.lst",
    ]
    for root in (src_dir / "scripts", src_dir / "scripts" / "pnglibconf", src_dir / "scripts" / "cmake"):
        if not root.is_dir():
            continue
        paths.extend(
            sorted(
                path
                for path in root.iterdir()
                if path.is_file()
                and (
                    path.suffix.lower() in {".awk", ".c", ".dfa", ".in", ".lst"}
                    or path.name.endswith(".cmake.in")
                )
            )
        )
    return paths


def pre_build(builder, _repo, ctx, _env) -> None:
    builder._normalize_posix_shell_scripts("libpng", _posix_line_ending_files(ctx.src_dir))
    builder._ensure_png16_include_alias(ctx.install_prefix)


def post_install(builder, install_prefix, _build_type: str) -> None:
    builder._ensure_png16_include_alias(install_prefix)
