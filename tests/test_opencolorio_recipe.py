from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from builder.core import Builder
from builder.platform import PlatformInfo
from builder.recipes import opencolorio


def recipe_args(prefix: Path, platform: str, mode: str) -> list[str]:
    builder = SimpleNamespace(
        platform=SimpleNamespace(os=platform),
        config=SimpleNamespace(global_cfg=SimpleNamespace(ocio_build_apps="OFF", windows={})),
        _ensure_ppmd_package=Mock(),
        _windows_python_wrappers_enabled=lambda: (True, ""),
    )
    return opencolorio.cmake_args(builder, SimpleNamespace(install_prefix=prefix, build_type=mode))


class OpenColorIORecipeTests(unittest.TestCase):
    def test_asan_configuration_list_preserves_other_modes_and_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            for platform in ("linux", "macos", "windows"):
                for mode in ("ASAN", "Release", "Debug"):
                    with self.subTest(platform=platform, mode=mode):
                        args = recipe_args(Path(tmp), platform, mode)
                        configs = [a for a in args if a.startswith("-DCMAKE_CONFIGURATION_TYPES=")]
                        self.assertEqual(bool(configs), platform != "windows" and mode == "ASAN")
                        if configs:
                            self.assertEqual(configs[0].split("=", 1)[1].split(";"),
                                             ["Debug", "Release", "MinSizeRel", "RelWithDebInfo", "ASAN"])
                        self.assertFalse(any(a.startswith("-DCMAKE_BUILD_TYPE=") for a in args))
                        self.assertIn("-DOCIO_BUILD_PYTHON=ON", args)
                        self.assertIn("-DOCIO_INSTALL_EXT_PACKAGES=NONE", args)

    def test_python_env_is_scoped_and_preserves_options(self):
        cases = (({}, {}, "detect_leaks=0"),
                 ({}, {"ASAN_OPTIONS": "halt_on_error=1"}, "halt_on_error=1:detect_leaks=0"),
                 ({}, {"ASAN_OPTIONS": "detect_leaks=1"}, "detect_leaks=1"),
                 ({"ASAN_OPTIONS": "halt_on_error=1:detect_leaks=1"}, {}, "halt_on_error=1:detect_leaks=1"),
                 ({"ASAN_OPTIONS": "detect_leaks=0"}, {"ASAN_OPTIONS": "detect_leaks=1"}, "detect_leaks=0"))
        for platform in ("linux", "macos", "windows"):
            for mode in ("Debug", "Release", "ASAN"):
                for env, parent, expected in cases:
                    with self.subTest(platform=platform, mode=mode, env=env, parent=parent):
                        with patch.dict(os.environ, parent, clear=True):
                            builder = object.__new__(Builder)
                            builder.platform = PlatformInfo(os=platform, arch="x86_64")
                            builder._env_for_build = lambda *_: dict(env)
                            actual = builder._env_for_repo_build(SimpleNamespace(name="OpenColorIO"), mode, Path("prefix"))
                            wanted = expected if platform != "windows" and mode == "ASAN" else env.get("ASAN_OPTIONS", parent.get("ASAN_OPTIONS"))
                            self.assertEqual(wanted, actual.get("ASAN_OPTIONS", os.environ.get("ASAN_OPTIONS")))
                            self.assertEqual(parent.get("ASAN_OPTIONS"), os.environ.get("ASAN_OPTIONS"))
                            self.assertEqual(env, builder._env_for_repo_build(SimpleNamespace(name="other"), mode, Path("prefix")))

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required")
    def test_configuration_override_passes_ocio_style_guard_without_renaming_asan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.20)
project(OcioConfigProbe LANGUAGES NONE)
if(NOT DEFINED CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_CONFIGURATION_TYPES "Debug;Release;MinSizeRel;RelWithDebInfo" CACHE STRING "" FORCE)
endif()
if(NOT CMAKE_BUILD_TYPE IN_LIST CMAKE_CONFIGURATION_TYPES)
  message(FATAL_ERROR "Unsupported build type")
endif()
if(NOT CMAKE_BUILD_TYPE STREQUAL "ASAN")
  message(FATAL_ERROR "ASAN configuration was renamed")
endif()
''')
            common = ["cmake", "-S", str(root), "-DCMAKE_BUILD_TYPE=ASAN"]
            failed = subprocess.run([*common, "-B", str(root / "baseline")], capture_output=True, text=True)
            self.assertNotEqual(0, failed.returncode)
            self.assertIn("Unsupported build type", failed.stdout + failed.stderr)
            fixed = subprocess.run([*common, "-B", str(root / "fixed"), *recipe_args(root, "linux", "ASAN")],
                                   capture_output=True, text=True)
            self.assertEqual(0, fixed.returncode, fixed.stdout + fixed.stderr)


if __name__ == "__main__":
    unittest.main()
