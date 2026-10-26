from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

_RAW_ERRNO_LOOKUP = """\
        // C style error code, use safe version of strerror
        const size_t maxbuf = 100;
        char buf[maxbuf]    = { 0 };
        if (strerror_r(ret, buf, maxbuf) == 0)
            msg = std::string(buf);
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

    def _rawinput_file(self, root: Path, contents: str) -> Path:
        rawinput = root / "src" / "raw.imageio" / "rawinput.cpp"
        rawinput.parent.mkdir(parents=True)
        rawinput.write_bytes(contents.encode("utf-8"))
        return rawinput

    def test_temporarily_patches_windows_raw_errno_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = "// unrelated prefix\n" + _RAW_ERRNO_LOOKUP + "// unrelated suffix\n"
            rawinput = self._rawinput_file(root, contents)

            openimageio._patch_rawinput_windows_strerror(root)
            patched = rawinput.read_text(encoding="utf-8")
            self.assertIn("TEMPORARY: remove when upstream", patched)
            self.assertIn(
                "#ifdef _WIN32\n"
                "        if (strerror_s(buf, maxbuf, ret) == 0)\n"
                "#else\n"
                "        if (strerror_r(ret, buf, maxbuf) == 0)\n"
                "#endif\n"
                "            msg = std::string(buf);",
                patched,
            )
            self.assertTrue(patched.startswith("// unrelated prefix\n"))
            self.assertTrue(patched.endswith("// unrelated suffix\n"))

            openimageio._patch_rawinput_windows_strerror(root)
            self.assertEqual(patched, rawinput.read_text(encoding="utf-8"))

    def test_skips_upstream_raw_errno_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = "// upstream no longer calls strerror_r\nmsg = portable_error(ret);\n"
            rawinput = self._rawinput_file(root, contents)

            openimageio._patch_rawinput_windows_strerror(root)

            self.assertEqual(contents, rawinput.read_text(encoding="utf-8"))

    def test_preserves_rawinput_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = ("// unrelated prefix\n" + _RAW_ERRNO_LOOKUP).replace("\n", "\r\n")
            rawinput = self._rawinput_file(root, contents)

            openimageio._patch_rawinput_windows_strerror(root)
            patched = rawinput.read_bytes()

            self.assertIn(b"strerror_s(buf, maxbuf, ret)", patched)
            self.assertNotIn(b"\n", patched.replace(b"\r\n", b""))
            self.assertTrue(patched.startswith(b"// unrelated prefix\r\n"))
            openimageio._patch_rawinput_windows_strerror(root)
            self.assertEqual(patched, rawinput.read_bytes())

    def test_rejects_unrecognized_unguarded_raw_errno_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = _RAW_ERRNO_LOOKUP.replace(
                "strerror_r(ret, buf, maxbuf)", "strerror_r(ret,buf,maxbuf)"
            )
            rawinput = self._rawinput_file(root, contents)

            with self.assertRaisesRegex(RuntimeError, "no longer matches upstream source"):
                openimageio._patch_rawinput_windows_strerror(root)

            self.assertEqual(contents, rawinput.read_text(encoding="utf-8"))

    def test_skips_upstream_windows_raw_errno_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = _RAW_ERRNO_LOOKUP.replace(
                "        if (strerror_r(ret, buf, maxbuf) == 0)",
                "#ifdef _WIN32\n"
                "        if (strerror_s(buf, maxbuf, ret) == 0)\n"
                "#else\n"
                "        if (strerror_r(ret, buf, maxbuf) == 0)\n"
                "#endif",
            )
            rawinput = self._rawinput_file(root, contents)

            openimageio._patch_rawinput_windows_strerror(root)

            self.assertEqual(contents, rawinput.read_text(encoding="utf-8"))

    def test_skips_missing_raw_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            openimageio._patch_rawinput_windows_strerror(Path(tmp))
            self.assertEqual([], list(Path(tmp).iterdir()))

    def test_dispatches_raw_errno_patch_only_for_non_dry_run_windows(self):
        helpers = (
            "_patch_compiled_fmt_option",
            "_patch_msvc_python_module_link",
            "_patch_nanobind_discovery_macro_return",
            "_patch_nanobind_find_package_range_check",
            "_patch_nanobind_msvc_debug_python",
            "_patch_giflib_windows_macro_leak",
            "_patch_static_robinmap_config",
        )
        for platform, dry_run in (("windows", False), ("linux", False), ("macos", False), ("windows", True)):
            with self.subTest(platform=platform, dry_run=dry_run), ExitStack() as stack:
                for helper in helpers:
                    stack.enter_context(patch.object(openimageio, helper))
                raw_patch = stack.enter_context(patch.object(openimageio, "_patch_rawinput_windows_strerror"))
                builder = SimpleNamespace(
                    dry_run=dry_run,
                    platform=SimpleNamespace(os=platform),
                    config=SimpleNamespace(global_cfg=SimpleNamespace(build_dng_sdk=False)),
                )
                root = Path("unused-source")

                openimageio.patch_source(builder, root)

                if platform == "windows" and not dry_run:
                    raw_patch.assert_called_once_with(root)
                else:
                    raw_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
