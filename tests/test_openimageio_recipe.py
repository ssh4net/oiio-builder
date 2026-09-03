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


if __name__ == "__main__":
    unittest.main()
