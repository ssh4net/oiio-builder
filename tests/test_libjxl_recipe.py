from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import libjxl


def _args(root: Path, platform: str, mode: str) -> list[str]:
    builder = SimpleNamespace(
        platform=SimpleNamespace(os=platform),
        config=SimpleNamespace(global_cfg=SimpleNamespace(
            build_exr_stack=True, libjxl_enable_tools="ON", windows={},
        )),
    )
    ctx = SimpleNamespace(build_type=mode, install_prefix=root)
    return libjxl.cmake_args(builder, ctx)


class LibjxlRecipeTests(unittest.TestCase):
    def test_openexr_rtti_patch_preserves_line_endings_and_is_idempotent(self):
        fixture = '''if(OpenEXR_FOUND)
      set(_exr_flags "-fcxx-exceptions")
      set_source_files_properties(extras/dec/exr.cc extras/enc/exr.cc
        PROPERTIES COMPILE_FLAGS "${_exr_flags}"
      )
endif()
'''
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "lib" / "jxl_extras.cmake"
                path.parent.mkdir()
                original = fixture.replace("\n", newline).encode()
                path.write_bytes(original)
                libjxl.patch_source(SimpleNamespace(dry_run=True), root)
                self.assertEqual(original, path.read_bytes())
                libjxl._patch_asan_openexr_rtti(root)
                updated = path.read_bytes()
                self.assertIn(b'string(APPEND _exr_flags " -frtti")', updated)
                self.assertIn(b'CMAKE_BUILD_TYPE STREQUAL "ASAN"', updated)
                self.assertIn(b'CMAKE_SYSTEM_NAME STREQUAL "Linux"', updated)
                self.assertIn(b'CMAKE_CXX_COMPILER_ID MATCHES "Clang"', updated)
                libjxl._patch_asan_openexr_rtti(root)
                self.assertEqual(updated, path.read_bytes())
                if newline == "\r\n":
                    self.assertNotIn(b"\n", updated.replace(b"\r\n", b""))

    def test_openexr_rtti_patch_rejects_source_drift_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "lib" / "jxl_extras.cmake"
            path.parent.mkdir()
            original = b"# Upstream changed its source property block.\n"
            path.write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                libjxl._patch_asan_openexr_rtti(root)
            self.assertEqual(original, path.read_bytes())

    def test_linux_asan_policy_preserves_tools_and_other_build_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for platform in ("linux", "macos", "windows"):
                for mode in ("Debug", "Release", "ASAN"):
                    with self.subTest(platform=platform, mode=mode):
                        args = _args(Path(tmp), platform, mode)
                        affected = platform == "linux" and mode == "ASAN"
                        self.assertIn(f"-DJPEGXL_STATIC={'OFF' if affected else 'ON'}", args)
                        for flag in ("-DBUILD_SHARED_LIBS=OFF", "-DJPEGXL_ENABLE_FUZZERS=OFF", "-DJPEGXL_ENABLE_TCMALLOC=OFF",
                                     "-DCMAKE_JOB_POOLS=libjxl_asan=2",
                                     "-DCMAKE_JOB_POOL_COMPILE=libjxl_asan",
                                     "-DCMAKE_JOB_POOL_LINK=libjxl_asan"):
                            self.assertEqual(affected, flag in args)
                        self.assertIn("-DJPEGXL_ENABLE_TOOLS=ON", args)
                        self.assertIn("-DJPEGXL_ENABLE_OPENEXR=ON", args)
                        self.assertFalse(any("BUILD_SHARED_LIBS=ON" in flag for flag in args))
                        self.assertFalse(any("ENABLE_HWY_" in flag for flag in args))

    @unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("cmake")
                         and shutil.which("ninja") and shutil.which("clang"),
                         "Linux CMake, Ninja, and Clang are needed for the ASAN link probe")
    def test_asan_threads_and_static_archive_work_with_bounded_ninja_pool(self):
        # Reproduce libjxl's distinction between static executables and archives.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.16)
project(AsanStaticProbe LANGUAGES C)
if(JPEGXL_STATIC)
  string(APPEND CMAKE_EXE_LINKER_FLAGS " -static")
endif()
set(THREADS_PREFER_PTHREAD_FLAG YES)
find_package(Threads REQUIRED)
add_library(probe probe.c)
target_link_libraries(probe PUBLIC Threads::Threads)
add_executable(consumer main.c)
target_link_libraries(consumer PRIVATE probe)
''')
            (source / "probe.c").write_text('''#include <pthread.h>
static void *worker(void *p) { return p; }
int probe(void) { pthread_t t; return pthread_create(&t, 0, worker, 0) || pthread_join(t, 0); }
''')
            (source / "main.c").write_text("int probe(void); int main(void) { return probe(); }\n")
            common = ["cmake", "-S", str(source), "-G", "Ninja", "-DBUILD_SHARED_LIBS=OFF",
                      f"-DCMAKE_C_COMPILER={shutil.which('clang')}",
                      "-DCMAKE_C_FLAGS=-fsanitize=address -fno-omit-frame-pointer -fPIC"]
            baseline = subprocess.run([*common, "-B", str(root / "baseline"), "-DJPEGXL_STATIC=ON"],
                                      capture_output=True, text=True)
            self.assertNotEqual(0, baseline.returncode)
            self.assertIn("Could NOT find Threads", baseline.stdout + baseline.stderr)
            build = root / "fixed"
            configured = subprocess.run([*common, "-B", str(build), *_args(root, "linux", "ASAN")],
                                        capture_output=True, text=True)
            self.assertEqual(0, configured.returncode, configured.stdout + configured.stderr)
            rules = (build / "CMakeFiles" / "rules.ninja").read_text()
            self.assertIn("pool libjxl_asan\n  depth = 2", rules)
            graph = (build / "build.ninja").read_text()
            self.assertEqual(4, graph.count("pool = libjxl_asan"))
            built = subprocess.run(["cmake", "--build", str(build), "--parallel", "16"],
                                   capture_output=True, text=True)
            self.assertEqual(0, built.returncode, built.stdout + built.stderr)
            self.assertTrue((build / "libprobe.a").is_file())


if __name__ == "__main__":
    unittest.main()
