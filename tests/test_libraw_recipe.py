from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.recipes import libraw


def _args(root: Path, platform: str, mode: str = "Debug") -> list[str]:
    cfg = SimpleNamespace(src_root=root, libraw_enable_examples="ON", libraw_enable_openmp="ON",
                          env={}, windows_env={}, windows={}, build_dng_sdk=False)
    builder = SimpleNamespace(config=SimpleNamespace(global_cfg=cfg), platform=SimpleNamespace(os=platform))
    ctx = SimpleNamespace(build_type=mode, install_prefix=root / "prefix", build_dir=root / "build")
    with patch.object(libraw, "resolve_openmp_root", return_value=None):
        return libraw.cmake_args(builder, ctx)


class LibrawRecipeTests(unittest.TestCase):
    def test_regeneration_policy_is_scoped_to_linux_and_preserves_feature_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for platform in ("linux", "macos", "windows"):
                for mode in ("Debug", "Release"):
                    with self.subTest(platform=platform, mode=mode):
                        args = _args(root, platform, mode)
                        self.assertEqual(platform == "linux", "-DCMAKE_SUPPRESS_REGENERATION=ON" in args)
                        self.assertIn("-DENABLE_EXAMPLES=ON", args)
                        self.assertIn("-DENABLE_OPENMP=ON", args)
                        self.assertIn("-DENABLE_RAWSPEED=OFF", args)
                        self.assertIn(f"-DENABLE_DCRAW_DEBUG={'ON' if mode == 'Debug' else 'OFF'}", args)

    @unittest.skipUnless(shutil.which("cmake") and shutil.which("ninja") and shutil.which("cc"),
                         "CMake, Ninja and a C compiler are needed for regeneration checks")
    def test_future_configure_input_cannot_loop_and_explicit_configure_rediscovers_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source with spaces"
            sources = root / "LibRaw/src"
            source.mkdir()
            sources.mkdir(parents=True)
            main = sources / "main.c"
            main.write_text("int main(void) { return 0; }\n")
            cmake = source / "CMakeLists.txt"
            cmake.write_text('''cmake_minimum_required(VERSION 3.12)
project(libraw_glob_fixture C)
set(count_file "${CMAKE_BINARY_DIR}/configure-count.txt")
if(EXISTS "${count_file}")
  file(STRINGS "${count_file}" configures)
  list(LENGTH configures count)
  if(count GREATER 4)
    message(FATAL_ERROR "Bounded test detected repeated CMake regeneration")
  endif()
endif()
file(APPEND "${count_file}" "configure\\n")
file(GLOB_RECURSE sources CONFIGURE_DEPENDS "${LIBRAW_PATH}/src/*.c")
add_executable(glob_consumer ${sources})
''')
            future = time.time() + 600
            os.utime(cmake, (future, future))
            build = root / "build"
            configure = ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja", *_args(root, "linux")]
            def run(command):
                result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                return result.stdout + result.stderr
            run(configure)
            manifest = (build / "build.ninja").read_text()
            self.assertNotIn("RERUN_CMAKE", manifest)
            self.assertNotIn("VERIFY_GLOBS", manifest)
            for _ in range(2):
                output = run(["cmake", "--build", str(build), "--parallel", "2"])
                self.assertNotIn("Re-running CMake", output)
            count_file = build / "configure-count.txt"
            self.assertEqual(["configure"], count_file.read_text().splitlines())
            # A new source is discovered at the next explicit configure.
            (sources / "extra.c").write_text("int extra(void) { return 42; }\n")
            main.write_text("int extra(void); int main(void) { return extra() == 42 ? 0 : 1; }\n")
            run(configure)
            run(["cmake", "--build", str(build), "--parallel", "2"])
            self.assertEqual(["configure", "configure"], count_file.read_text().splitlines())
            binary = build / ("glob_consumer.exe" if os.name == "nt" else "glob_consumer")
            self.assertEqual(0, subprocess.run([str(binary)]).returncode)


if __name__ == "__main__":
    unittest.main()
