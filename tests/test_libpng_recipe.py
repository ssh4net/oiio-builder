from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import libpng


def _fixture(source: Path, newline: str = "\n") -> Path:
    source.mkdir(parents=True)
    text = '''cmake_minimum_required(VERSION 3.14)
project(png_alias_fixture C)
# Existing user text and the shared-library alias must survive.
set(PNGLIB_ABI_VERSION 18)
function(create_symlink DEST_FILE)
  message(FATAL_ERROR "Unexpected symlink alias: ${DEST_FILE}")
endfunction()
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/libpng18.pc" "prefix=${CMAKE_INSTALL_PREFIX}\\n")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/libpng18-config" "#!/bin/sh\\necho png-alias-fixture\\n")
create_symlink(libpng.pc FILE libpng${PNGLIB_ABI_VERSION}.pc)
create_symlink(libpng-config FILE libpng${PNGLIB_ABI_VERSION}-config)
add_library(png_static STATIC png.c)
set_target_properties(png_static PROPERTIES OUTPUT_NAME png18 DEBUG_POSTFIX d)
create_symlink(libpng${CMAKE_STATIC_LIBRARY_SUFFIX} TARGET png_static)
if(PNG_SHARED)
  create_symlink(libpng${CMAKE_SHARED_LIBRARY_SUFFIX} TARGET png_shared)
endif()
install(TARGETS png_static ARCHIVE DESTINATION lib)
install(FILES "$<TARGET_LINKER_FILE_DIR:png_static>/libpng${CMAKE_STATIC_LIBRARY_SUFFIX}"
        DESTINATION lib)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/libpng.pc" DESTINATION lib/pkgconfig)
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/libpng-config" DESTINATION bin)
'''
    cmake_lists = source / "CMakeLists.txt"
    cmake_lists.write_bytes(text.replace("\n", newline).encode())
    return cmake_lists


class LibpngRecipeTests(unittest.TestCase):
    def test_patch_is_idempotent_and_preserves_shared_alias_and_line_endings(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "source"
                path = _fixture(source, newline)
                libpng.patch_source(SimpleNamespace(dry_run=False), source)
                first = path.read_bytes()
                libpng.patch_source(SimpleNamespace(dry_run=False), source)
                self.assertEqual(first, path.read_bytes())
                self.assertIn(b"# Existing user text", first)
                self.assertIn(b"create_symlink(libpng${CMAKE_SHARED_LIBRARY_SUFFIX} TARGET png_shared)", first)
                self.assertIn(b"function(create_symlink DEST_FILE)", first)
                self.assertNotIn(b"create_symlink(libpng${CMAKE_STATIC_LIBRARY_SUFFIX}", first)
                if newline == "\r\n":
                    self.assertNotIn(b"\n", first.replace(b"\r\n", b""))

    def test_dry_run_and_missing_sources_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source"
            path = _fixture(source)
            original = path.read_bytes()
            libpng.patch_source(SimpleNamespace(dry_run=True), source)
            self.assertEqual(original, path.read_bytes())
            libpng.patch_source(SimpleNamespace(dry_run=False), source / "missing")

    @unittest.skipUnless(os.name != "nt" and shutil.which("cmake") and shutil.which("cc"),
                         "POSIX CMake and a C compiler are needed for installed aliases")
    def test_build_install_and_rebuild_replace_legacy_aliases_with_usable_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source with spaces"
            _fixture(source)
            libpng.patch_source(SimpleNamespace(dry_run=False), source)
            build = root / "build with spaces"
            build.mkdir()
            prefix = root / "prefix with spaces"
            for alias, target in (("libpng.pc", "libpng18.pc"),
                                  ("libpng-config", "libpng18-config"),
                                  ("libpng.a", "libpng18d.a")):
                (build / alias).symlink_to(target)
            (source / "png.c").write_text("int png_alias_answer(void) { return 73; }\n")
            main = root / "main.c"
            main.write_text("#include <stdio.h>\nint png_alias_answer(void);\n"
                            "int main(void) { printf(\"%d\\n\", png_alias_answer()); return 0; }\n")

            def run(command: list[str]) -> str:
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                return result.stdout

            run(["cmake", "-S", str(source), "-B", str(build), "-DCMAKE_BUILD_TYPE=Debug",
                 f"-DCMAKE_INSTALL_PREFIX={prefix}"])
            for answer in (73, 74):
                (source / "png.c").write_text(f"int png_alias_answer(void) {{ return {answer}; }}\n")
                # Also exercise replacement when an old alias reappears before a rebuild.
                if answer == 74:
                    (build / "libpng.a").unlink()
                    (build / "libpng.a").symlink_to("libpng18d.a")
                run(["cmake", "--build", str(build), "--parallel", "2"])
                alias = build / "libpng.a"
                self.assertFalse(alias.is_symlink())
                self.assertEqual((build / "libpng18d.a").read_bytes(), alias.read_bytes())
                executable = root / "consumer"
                run(["cc", str(main), str(alias), "-o", str(executable)])
                self.assertEqual(str(answer), run([str(executable)]).strip())
            run(["cmake", "--install", str(build)])
            for relative in ("lib/libpng.a", "lib/pkgconfig/libpng.pc", "bin/libpng-config"):
                self.assertTrue((prefix / relative).is_file())
                self.assertFalse((prefix / relative).is_symlink())
            self.assertEqual((prefix / "lib/libpng18d.a").read_bytes(),
                             (prefix / "lib/libpng.a").read_bytes())
            self.assertIn(str(prefix), (prefix / "lib/pkgconfig/libpng.pc").read_text())
            config = prefix / "bin/libpng-config"
            self.assertTrue(os.access(config, os.X_OK))
            self.assertEqual("png-alias-fixture", run([str(config)]).strip())


if __name__ == "__main__":
    unittest.main()
