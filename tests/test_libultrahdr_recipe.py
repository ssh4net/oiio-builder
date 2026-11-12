from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import libultrahdr


POLICY = '''if(DEFINED UHDR_SANITIZE_OPTIONS AND NOT UHDR_BUILD_DEPS)
  set(UHDR_BUILD_DEPS TRUE)
endif()
if(UHDR_BUILD_DEPS AND UHDR_ENABLE_INSTALL)
  set(UHDR_ENABLE_INSTALL FALSE)
endif()
if(APPLE)
  set(UHDR_ENABLE_STATIC_LINKING OFF)
  elseif(DEFINED UHDR_SANITIZE_OPTIONS OR UHDR_BUILD_FUZZERS)
  set(UHDR_ENABLE_STATIC_LINKING OFF)
else()
  set(UHDR_ENABLE_STATIC_LINKING ON)
endif()
'''


class LibultrahdrRecipeTests(unittest.TestCase):
    def test_patch_preserves_line_endings_and_is_idempotent_and_dry_run_safe(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "CMakeLists.txt"
                original = ("cmake_policy(SET CMP0091 OLD)\n" + POLICY).replace("\n", newline).encode()
                path.write_bytes(original)
                libultrahdr.patch_source(SimpleNamespace(dry_run=True), root)
                self.assertEqual(original, path.read_bytes())
                libultrahdr.patch_source(SimpleNamespace(dry_run=False), root)
                patched = path.read_bytes()
                self.assertIn(b"cmake_policy(SET CMP0091 NEW)", patched)
                self.assertIn(b'CMAKE_SYSTEM_NAME STREQUAL "Linux" AND CMAKE_BUILD_TYPE STREQUAL "ASAN"', patched)
                libultrahdr.patch_source(SimpleNamespace(dry_run=False), root)
                self.assertEqual(patched, path.read_bytes())
                if newline == "\r\n":
                    self.assertNotIn(b"\n", patched.replace(b"\r\n", b""))

    def test_source_drift_fails_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "CMakeLists.txt"
            original = b"cmake_policy(SET CMP0091 OLD)\n# Changed upstream policy\n"
            path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                libultrahdr.patch_source(SimpleNamespace(dry_run=False), root)
            self.assertEqual(original, path.read_bytes())

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cmake")
                         and shutil.which("ninja") and shutil.which("clang"),
                         "Linux CMake, Ninja, and Clang are needed for the ASAN link probe")
    def test_asan_app_links_with_static_archive_and_installed_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.20)
project(UhdrAsanProbe LANGUAGES C)
set(BUILD_SHARED_LIBS OFF)
set(UHDR_BUILD_DEPS OFF)
set(UHDR_ENABLE_INSTALL ON)
''' + POLICY + '''
if(UHDR_BUILD_DEPS OR NOT UHDR_ENABLE_INSTALL)
  message(FATAL_ERROR "Prefix dependency or install policy changed")
endif()
add_library(codec codec.c)
add_executable(app main.c)
target_link_libraries(app PRIVATE codec)
if(UHDR_ENABLE_STATIC_LINKING AND NOT MSVC)
  target_link_options(app PRIVATE -static)
endif()
''')
            (source / "codec.c").write_text("int codec(void) { return 0; }\n")
            (source / "main.c").write_text("int codec(void); int main(void) { return codec(); }\n")
            common = ["cmake", "-S", str(source), "-G", "Ninja",
                      f"-DCMAKE_C_COMPILER={shutil.which('clang')}"]
            asan = ["-DCMAKE_BUILD_TYPE=ASAN", "-DCMAKE_C_FLAGS=-fsanitize=address -fno-omit-frame-pointer"]
            baseline = root / "baseline"
            configured = subprocess.run([*common, "-B", str(baseline), *asan], capture_output=True, text=True)
            self.assertEqual(0, configured.returncode, configured.stdout + configured.stderr)
            failed = subprocess.run(["cmake", "--build", str(baseline)], capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertIn("_DYNAMIC", failed.stdout + failed.stderr)
            libultrahdr.patch_source(SimpleNamespace(dry_run=False), source)
            for mode in ("ASAN", "Release"):
                build = root / mode
                flags = asan if mode == "ASAN" else ["-DCMAKE_BUILD_TYPE=Release"]
                configured = subprocess.run([*common, "-B", str(build), *flags], capture_output=True, text=True)
                self.assertEqual(0, configured.returncode, configured.stdout + configured.stderr)
                graph = (build / "build.ninja").read_text()
                self.assertEqual(mode == "Release", "LINK_FLAGS = -static" in graph)
                if mode == "ASAN":
                    built = subprocess.run(["cmake", "--build", str(build)], capture_output=True, text=True)
                    self.assertEqual(0, built.returncode, built.stdout + built.stderr)
                    self.assertTrue((build / "libcodec.a").is_file())


if __name__ == "__main__":
    unittest.main()
