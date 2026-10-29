from __future__ import annotations

from pathlib import Path


STAMP_REVISION = "2"


def _patch_file(path: Path, replacements: list[tuple[str, str]]) -> None:
    if not path.is_file():
        return
    with path.open(encoding="utf-8", newline="") as source:
        original = source.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    updated = text
    for before, after in replacements:
        updated = updated.replace(before, after)
    if updated != text:
        with path.open("w", encoding="utf-8", newline="") as source:
            source.write(updated.replace("\n", newline))


def patch_source(builder, src_dir: Path) -> None:
    if getattr(builder, "dry_run", False):
        return
    cmake_dir = src_dir / "build" / "cmake"
    _patch_file(cmake_dir / "CMakeModules" / "AddZstdCompilationFlags.cmake", [
        ("if(CMAKE_CXX_COMPILER)\n", "if(CMAKE_CXX_COMPILER_LOADED)\n"),
        ("    if (_CXX AND CMAKE_CXX_COMPILER)\n", "    if (_CXX AND CMAKE_CXX_COMPILER_LOADED)\n"),
    ])

    # Keep command and manual aliases available on filesystems without symlinks.
    # Executable aliases must retain their permissions when installed as files.
    replacements = []
    for alias, dependency, source in (
        ("zstdcat", "zstd", "$<TARGET_FILE:zstd>"),
        ("unzstd", "zstd", "$<TARGET_FILE:zstd>"),
        ("zstdmt", "zstd", "$<TARGET_FILE:zstd>"),
        ("zstdcat.1", "zstd.1", "${CMAKE_CURRENT_BINARY_DIR}/zstd.1"),
        ("unzstd.1", "zstd.1", "${CMAKE_CURRENT_BINARY_DIR}/zstd.1"),
    ):
        before = (
            f"add_custom_target({alias} ALL ${{CMAKE_COMMAND}} -E create_symlink "
            f'{dependency} {alias} DEPENDS {dependency} COMMENT "Creating {alias} symlink")'
        )
        after = (
            f"add_custom_target({alias} ALL\n"
            '    COMMAND "${CMAKE_COMMAND}" -E copy_if_different\n'
            f'        "{source}" "${{CMAKE_CURRENT_BINARY_DIR}}/{alias}"\n'
            f'    DEPENDS {dependency} COMMENT "Copying {alias} alias" VERBATIM)'
        )
        replacements.append((before, after))
        if dependency == "zstd":
            replacements.append((
                f'install(FILES ${{CMAKE_CURRENT_BINARY_DIR}}/{alias} DESTINATION "${{CMAKE_INSTALL_BINDIR}}")',
                f'install(PROGRAMS "${{CMAKE_CURRENT_BINARY_DIR}}/{alias}" DESTINATION "${{CMAKE_INSTALL_BINDIR}}")',
            ))
    _patch_file(cmake_dir / "programs" / "CMakeLists.txt", replacements)
