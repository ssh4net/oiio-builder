from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import zstd


def _fixture(root: Path, newline: str = "\n") -> tuple[Path, Path]:
    cmake = root / "build" / "cmake"
    programs = cmake / "programs"
    programs.mkdir(parents=True)
    flags = cmake / "CMakeModules" / "AddZstdCompilationFlags.cmake"
    flags.parent.mkdir()
    flags.write_bytes(("if(CMAKE_CXX_COMPILER)\nendif()\n"
                       "    if (_CXX AND CMAKE_CXX_COMPILER)\n    endif()\n").replace("\n", newline).encode())
    text = (
        "# existing user text\n"
        "add_executable(zstd main.c)\n"
        'install(TARGETS zstd RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")\n'
        "if(UNIX)\n"
    )
    for alias in ("zstdcat", "unzstd"):
        text += (
            f"add_custom_target({alias} ALL ${{CMAKE_COMMAND}} -E create_symlink zstd {alias} "
            f'DEPENDS zstd COMMENT "Creating {alias} symlink")\n'
            f'install(FILES ${{CMAKE_CURRENT_BINARY_DIR}}/{alias} DESTINATION "${{CMAKE_INSTALL_BINDIR}}")\n'
        )
    text += ('add_custom_target(zstd.1 ALL COMMAND "${CMAKE_COMMAND}" -E copy '
             '"${CMAKE_CURRENT_SOURCE_DIR}/zstd.1" "${CMAKE_CURRENT_BINARY_DIR}/zstd.1")\n')
    for alias in ("zstdcat.1", "unzstd.1"):
        text += (
            f"add_custom_target({alias} ALL ${{CMAKE_COMMAND}} -E create_symlink zstd.1 {alias} "
            f'DEPENDS zstd.1 COMMENT "Creating {alias} symlink")\n'
            f'install(FILES ${{CMAKE_CURRENT_BINARY_DIR}}/{alias} DESTINATION "share/man/man1")\n'
        )
    text += (
        "if(ZSTD_MULTITHREAD_SUPPORT)\n"
        'add_custom_target(zstdmt ALL ${CMAKE_COMMAND} -E create_symlink zstd zstdmt DEPENDS zstd COMMENT "Creating zstdmt symlink")\n'
        'install(FILES ${CMAKE_CURRENT_BINARY_DIR}/zstdmt DESTINATION "${CMAKE_INSTALL_BINDIR}")\n'
        "endif()\nendif()\n# trailing user text\n"
    )
    path = programs / "CMakeLists.txt"
    path.write_bytes(text.replace("\n", newline).encode())
    return flags, path


class ZstdRecipeTests(unittest.TestCase):
    def test_patches_preserve_line_endings_user_text_and_are_idempotent(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                flags, programs = _fixture(root, newline)
                zstd.patch_source(SimpleNamespace(dry_run=False), root)
                before = {path: path.read_bytes() for path in (flags, programs)}
                zstd.patch_source(SimpleNamespace(dry_run=False), root)
                self.assertEqual(before, {path: path.read_bytes() for path in before})
                self.assertIn(b"CMAKE_CXX_COMPILER_LOADED", before[flags])
                self.assertNotIn(b"create_symlink", before[programs])
                self.assertEqual(3, before[programs].count(b"install(PROGRAMS"))
                self.assertIn(b"# existing user text", before[programs])
                self.assertIn(b"# trailing user text", before[programs])
                if newline == "\r\n":
                    for content in before.values():
                        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))

    def test_dry_run_does_not_patch_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _fixture(root)
            before = {path: path.read_bytes() for path in paths}
            zstd.patch_source(SimpleNamespace(dry_run=True), root)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})

    @unittest.skipUnless(os.name != "nt" and shutil.which("cmake") and shutil.which("cc"),
                         "POSIX CMake and a C compiler are needed for installed aliases")
    def test_cmake_builds_and_installs_executable_copies_and_manpages(self):
        for multithread in (True, False):
            with self.subTest(multithread=multithread), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source with spaces"
                _, programs = _fixture(source)
                programs.parent.joinpath("main.c").write_text(
                    "#include <stdio.h>\nint main(int argc, char **argv) { (void)argc; puts(argv[0]); return 0; }\n",
                    encoding="utf-8",
                )
                programs.parent.joinpath("zstd.1").write_text("test manpage\n", encoding="utf-8")
                cmake = source / "build" / "cmake"
                (cmake / "CMakeLists.txt").write_text(
                    "cmake_minimum_required(VERSION 3.10)\nproject(zstd_alias_fixture C)\n"
                    "include(GNUInstallDirs)\nadd_subdirectory(programs)\n", encoding="utf-8",
                )
                zstd.patch_source(SimpleNamespace(dry_run=False), source)
                build = root / "build"
                prefix = root / "prefix"
                for command in (
                    ["cmake", "-S", str(cmake), "-B", str(build), f"-DCMAKE_INSTALL_PREFIX={prefix}",
                     f"-DZSTD_MULTITHREAD_SUPPORT={'ON' if multithread else 'OFF'}"],
                    ["cmake", "--build", str(build), "--parallel", "2"],
                    ["cmake", "--install", str(build)],
                ):
                    run = subprocess.run(command, capture_output=True, text=True)
                    self.assertEqual(0, run.returncode, run.stdout + run.stderr)
                aliases = ("zstdcat", "unzstd", "zstdmt") if multithread else ("zstdcat", "unzstd")
                for name in aliases:
                    installed = prefix / "bin" / name
                    self.assertFalse(installed.is_symlink())
                    self.assertTrue(os.access(installed, os.X_OK))
                    output = subprocess.check_output([str(installed)], text=True).strip()
                    self.assertEqual(str(installed), output)
                self.assertEqual(multithread, (prefix / "bin" / "zstdmt").exists())
                for name in ("zstdcat.1", "unzstd.1"):
                    installed = prefix / "share/man/man1" / name
                    self.assertFalse(installed.is_symlink())
                    self.assertEqual("test manpage\n", installed.read_text())


if __name__ == "__main__":
    unittest.main()
