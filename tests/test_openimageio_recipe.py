from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from builder.recipes import openimageio


_LEGACY_NANOBIND_LOOKUP = """\
    checked_find_package (nanobind CONFIG REQUIRED
                          VERSION_MIN 2.8.0 VERSION_MAX 3.9
                          BUILD_LOCAL missing)
"""

_CURRENT_NANOBIND_LOOKUP = """\
    discover_nanobind_cmake_dir()
    checked_find_package (nanobind CONFIG REQUIRED)
"""

_NANOBIND_DISCOVERY_MACRO = """\
macro (discover_nanobind_cmake_dir)
    if (nanobind_DIR OR nanobind_ROOT OR "$ENV{nanobind_DIR}" OR "$ENV{nanobind_ROOT}")
        return()
    endif ()

    if (NOT Python3_Interpreter_FOUND)
        return()
    endif ()

    execute_process (
        COMMAND ${Python3_EXECUTABLE} -m nanobind --cmake_dir
        RESULT_VARIABLE _oiio_nanobind_result
        OUTPUT_VARIABLE _oiio_nanobind_cmake_dir
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET)
    if (_oiio_nanobind_result EQUAL 0
            AND EXISTS "${_oiio_nanobind_cmake_dir}/nanobind-config.cmake")
        set (nanobind_DIR "${_oiio_nanobind_cmake_dir}" CACHE PATH
             "Path to the nanobind CMake package" FORCE)
    endif ()
endmacro()
"""


class OpenImageIORecipeTests(unittest.TestCase):
    def _externalpackages_file(self, root: Path, contents: str) -> Path:
        cmake_file = root / "src" / "cmake" / "externalpackages.cmake"
        cmake_file.parent.mkdir(parents=True)
        cmake_file.write_text(contents, encoding="utf-8")
        return cmake_file

    def test_patches_legacy_nanobind_version_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmake_file = self._externalpackages_file(Path(tmp), _LEGACY_NANOBIND_LOOKUP)

            openimageio._patch_nanobind_find_package_range_check(Path(tmp))
            patched = cmake_file.read_text(encoding="utf-8")
            self.assertIn("NO_FP_RANGE_CHECK", patched)

            openimageio._patch_nanobind_find_package_range_check(Path(tmp))
            self.assertEqual(patched, cmake_file.read_text(encoding="utf-8"))

    def test_skips_current_unversioned_nanobind_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmake_file = self._externalpackages_file(Path(tmp), _CURRENT_NANOBIND_LOOKUP)

            openimageio._patch_nanobind_find_package_range_check(Path(tmp))

            self.assertEqual(_CURRENT_NANOBIND_LOOKUP, cmake_file.read_text(encoding="utf-8"))

    def test_rejects_an_unrecognized_nanobind_version_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._externalpackages_file(
                Path(tmp),
                "checked_find_package (nanobind CONFIG REQUIRED VERSION_MIN 4.0)\n",
            )

            with self.assertRaisesRegex(RuntimeError, "no longer matches upstream source"):
                openimageio._patch_nanobind_find_package_range_check(Path(tmp))

    def test_rewrites_return_from_nanobind_discovery_macro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pythonutils = root / "src" / "cmake" / "pythonutils.cmake"
            pythonutils.parent.mkdir(parents=True)
            pythonutils.write_text(_NANOBIND_DISCOVERY_MACRO, encoding="utf-8")

            openimageio._patch_nanobind_discovery_macro_return(root)
            patched = pythonutils.read_text(encoding="utf-8")
            self.assertIn("OIIO_BUILDER_NANOBIND_DISCOVERY_NO_RETURN", patched)
            self.assertNotRegex(patched, r"(?m)^\s*return\(\)")
            self.assertIn("AND Python3_Interpreter_FOUND", patched)

            openimageio._patch_nanobind_discovery_macro_return(root)
            self.assertEqual(patched, pythonutils.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
