from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import openexr


_OPENJPH_SETUP = """\
set(UNRELATED_BEFORE "preserved")
#######################################
# Find or download OpenJPH
#######################################
option(OPENEXR_FORCE_INTERNAL_OPENJPH "Force internal OpenJPH" OFF)
set(OPENEXR_USE_INTERNAL_OPENJPH 0 CACHE INTERNAL "Bundled OpenJPH")
if (NOT OPENEXR_FORCE_INTERNAL_OPENJPH)
  find_package(openjph 0.32.0 CONFIG QUIET)
  if(openjph_FOUND)
    set(EXR_OPENJPH_LIB openjph)
  else()
    find_package(PkgConfig)
    pkg_check_modules(openjph IMPORTED_TARGET GLOBAL QUIET openjph=0.32.0)
    set(EXR_OPENJPH_LIB PkgConfig::openjph)
  endif()
endif()
if(EXR_OPENJPH_LIB)
  # Using external library
  set(EXR_OPENJPH_PKGCONFIG_REQUIRES "openjph >= 0.32.0")
else()
  set(OPENEXR_USE_INTERNAL_OPENJPH 1 CACHE INTERNAL "Bundled OpenJPH" FORCE)
  message(FATAL_ERROR "vendored OpenJPH fallback was entered")
endif()

#######################################
# Find or install Imath
#######################################
set(UNRELATED_AFTER "preserved")
"""

_OPENJPH_CONFIG = """\
# unrelated dependency before
if ("@EXR_OPENJPH_LIB@" STREQUAL "PkgConfig::openjph")
  if (NOT @BUILD_SHARED_LIBS@)
    openexr_find_pkgconfig_dependency(openjph "openjph=@openjph_VERSION@")
  endif()
elseif (NOT @OPENEXR_USE_INTERNAL_OPENJPH@)
  find_dependency(openjph)
endif()
# unrelated dependency after
"""

_ISOLATED_FIND_SETTINGS = """\
set(CMAKE_FIND_USE_CMAKE_SYSTEM_PATH FALSE)
set(CMAKE_FIND_USE_SYSTEM_ENVIRONMENT_PATH FALSE)
set(CMAKE_FIND_USE_PACKAGE_REGISTRY FALSE)
set(CMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY FALSE)
set(CMAKE_FIND_USE_CMAKE_ENVIRONMENT_PATH FALSE)
"""


class OpenEXRRecipeTests(unittest.TestCase):
    def _source(self, root: Path, contents: str = _OPENJPH_SETUP, newline: str = "\n") -> tuple[Path, Path]:
        cmake = root / "cmake"
        cmake.mkdir()
        setup = cmake / "OpenEXRSetup.cmake"
        config = cmake / "OpenEXRConfig.cmake.in"
        setup.write_bytes(contents.replace("\n", newline).encode("utf-8"))
        config.write_bytes(_OPENJPH_CONFIG.replace("\n", newline).encode("utf-8"))
        return setup, config

    def test_system_policy_is_idempotent_and_preserves_line_endings(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                setup, config = self._source(root, newline=newline)
                openexr._patch_system_openjph(root)
                patched = setup.read_bytes()
                patched_config = config.read_bytes()
                self.assertIn(b"find_package(openjph CONFIG REQUIRED)", patched)
                self.assertIn(b'_oiio_openjph_min_version "0.32.0"', patched)
                self.assertIn(b'0 CACHE INTERNAL "Use installed OpenJPH" FORCE', patched)
                self.assertNotIn(b"find_package(openjph 0.", patched)
                self.assertNotIn(b"pkg_check_modules", patched)
                self.assertNotIn(b"vendored OpenJPH fallback", patched)
                self.assertNotIn(b"TEMPORARY", patched)
                self.assertIn(b'UNRELATED_BEFORE "preserved"', patched)
                self.assertIn(b'UNRELATED_AFTER "preserved"', patched)
                self.assertIn(b"find_dependency(openjph @OIIO_BUILDER_OPENJPH_VERSION@ CONFIG)", patched_config)
                self.assertNotIn(b"PkgConfig::openjph", patched_config)
                self.assertIn(b"# unrelated dependency before", patched_config)
                self.assertIn(b"# unrelated dependency after", patched_config)
                if newline == "\r\n":
                    self.assertNotIn(b"\n", patched.replace(b"\r\n", b""))
                    self.assertNotIn(b"\n", patched_config.replace(b"\r\n", b""))
                openexr._patch_system_openjph(root)
                self.assertEqual(patched, setup.read_bytes())
                self.assertEqual(patched_config, config.read_bytes())

    def test_replaces_previous_033_workaround_instead_of_stacking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = _OPENJPH_SETUP.replace(
                "  find_package(openjph 0.32.0 CONFIG QUIET)\n",
                "  find_package(openjph 0.32.0 CONFIG QUIET)\n"
                "  # OIIO_BUILDER_OPENJPH_033_BEGIN\n"
                "  if(NOT openjph_FOUND)\n"
                "    find_package(openjph 0.33.0 CONFIG QUIET)\n"
                "  endif()\n"
                "  # OIIO_BUILDER_OPENJPH_033_END\n",
            )
            setup, config = self._source(root, previous)
            config.write_text(_OPENJPH_CONFIG.replace(
                "find_dependency(openjph)", "find_dependency(openjph @openjph_VERSION@ CONFIG)"
            ), encoding="utf-8")
            openexr._patch_system_openjph(root)
            self.assertNotIn("OPENJPH_033", setup.read_text(encoding="utf-8"))
            self.assertEqual(1, setup.read_text(encoding="utf-8").count("find_package(openjph CONFIG REQUIRED)"))
            self.assertNotIn('@MINIMUM_VERSION@', setup.read_text(encoding="utf-8"))

    def test_missing_or_legacy_setup_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            openexr._patch_system_openjph(root)
            self.assertEqual([], list(root.iterdir()))
            setup, config = self._source(root, "# Older OpenEXR with no OpenJPH dependency\n")
            before = setup.read_bytes(), config.read_bytes()
            openexr._patch_system_openjph(root)
            self.assertEqual(before, (setup.read_bytes(), config.read_bytes()))

    def test_rejects_unknown_discovery_layout_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup, config = self._source(root, "find_package(openjph 0.40 CONFIG QUIET)\n")
            before = setup.read_bytes(), config.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "no longer matches upstream source"):
                openexr._patch_system_openjph(root)
            self.assertEqual(before, (setup.read_bytes(), config.read_bytes()))

    def test_rejects_unknown_export_layout_before_writing_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup, config = self._source(root)
            config.write_text("# unexpected export layout\n", encoding="utf-8")
            before = setup.read_bytes(), config.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "export patch no longer matches"):
                openexr._patch_system_openjph(root)
            self.assertEqual(before, (setup.read_bytes(), config.read_bytes()))

    def test_rejects_a_separate_unrecognized_upstream_minimum_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            contents = _OPENJPH_SETUP.replace(
                "find_package(openjph 0.32.0 CONFIG QUIET)",
                "find_package(openjph CONFIG QUIET)\n"
                "  if(openjph_VERSION VERSION_LESS 0.40.0)\n"
                '    message(FATAL_ERROR "OpenJPH is too old")\n'
                "  endif()",
            )
            setup, config = self._source(root, contents)
            before = setup.read_bytes(), config.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "Cannot determine OpenEXR's OpenJPH requirement"):
                openexr._patch_system_openjph(root)
            self.assertEqual(before, (setup.read_bytes(), config.read_bytes()))

    def test_rejects_a_corrupted_permanent_minimum_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup, config = self._source(root)
            openexr._patch_system_openjph(root)
            setup.write_text(setup.read_text(encoding="utf-8").replace(
                'set(_oiio_openjph_min_version "0.32.0")', "# minimum was lost"
            ), encoding="utf-8")
            before = setup.read_bytes(), config.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "Cannot determine OpenEXR's OpenJPH requirement"):
                openexr._patch_system_openjph(root)
            self.assertEqual(before, (setup.read_bytes(), config.read_bytes()))

    def test_policy_also_runs_for_non_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            setup, _ = self._source(root)
            openexr.patch_source(SimpleNamespace(platform=SimpleNamespace(os="linux")), root)
            self.assertIn("SYSTEM_OPENJPH_BEGIN", setup.read_text(encoding="utf-8"))

    def _configure_probe(self, root: Path, version: str | None, *, minimum: str = "0.32.0",
                         target: bool = True, report_version: bool = True, force_internal: bool = False,
                         reported_version: str | None = None):
        setup, config = self._source(root, _OPENJPH_SETUP.replace("0.32.0", minimum))
        openexr._patch_system_openjph(root)
        package = root / "package"
        package.mkdir()
        cmake = "cmake_minimum_required(VERSION 3.14)\nproject(probe LANGUAGES NONE)\n"
        cmake += "include(CMakePackageConfigHelpers)\n"
        if version is not None:
            package_config = "add_library(openjph INTERFACE IMPORTED)\n" if target else "# missing target\n"
            if reported_version is not None:
                package_config += f'set(openjph_VERSION "{reported_version}")\n'
            (package / "openjph-config.cmake").write_text(
                package_config, encoding="utf-8"
            )
            if report_version:
                cmake += (
                    f'write_basic_package_version_file("{package.as_posix()}/openjph-config-version.cmake" '
                    f'VERSION "{version}" COMPATIBILITY SameMinorVersion)\n'
                )
        cmake += _ISOLATED_FIND_SETTINGS
        cmake += (
            f'set(CMAKE_PREFIX_PATH "{package.as_posix()}")\n'
            'set(OPENEXR_USE_INTERNAL_OPENJPH 1 CACHE INTERNAL "Previous fallback" FORCE)\n'
            f'set(OPENEXR_FORCE_INTERNAL_OPENJPH {"ON" if force_internal else "OFF"})\n'
            f'include("{setup.as_posix()}")\n'
            'if(OPENEXR_USE_INTERNAL_OPENJPH OR NOT EXR_OPENJPH_LIB STREQUAL "openjph")\n'
            '  message(FATAL_ERROR "Expected external OpenJPH and cleared fallback cache")\nendif()\n'
            f'configure_file("{config.as_posix()}" "${{CMAKE_BINARY_DIR}}/OpenEXRConfig.cmake" @ONLY)\n'
            'file(WRITE "${CMAKE_BINARY_DIR}/OpenEXR.pc" "Requires.private: ${EXR_OPENJPH_PKGCONFIG_REQUIRES}\\n")\n'
        )
        (root / "CMakeLists.txt").write_text(cmake, encoding="utf-8")
        result = subprocess.run(["cmake", "-S", str(root), "-B", str(root / "build")], capture_output=True, text=True)
        return result, root / "build"

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required for package discovery checks")
    def test_installed_newer_minors_are_accepted_and_exported(self):
        for version in ("0.32.0", "0.33.0", "0.34.2", "0.99.1", "1.0.0"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                result, build = self._configure_probe(Path(tmp), version)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"find_dependency(openjph {version} CONFIG)", (build / "OpenEXRConfig.cmake").read_text())
                self.assertIn(f"openjph >= {version}", (build / "OpenEXR.pc").read_text())

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required for package discovery checks")
    def test_invalid_packages_fail_instead_of_vendoring(self):
        cases = (
            ({"version": "0.31.9"}, "OpenEXR requires OpenJPH >= 0.32.0"),
            ({"version": None}, "Could not find a package configuration file provided by"),
            ({"version": "0.34.0", "target": False}, "must define target openjph"),
            ({"version": "0.34.0", "report_version": False}, "must report its version"),
            ({"version": "0.34.0", "reported_version": "999.invalid"}, "must report a numeric version"),
            ({"version": "0.34.0", "force_internal": True}, "bundled OpenJPH is disabled"),
        )
        for options, error in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                result, _ = self._configure_probe(Path(tmp), **options)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(error, result.stdout + result.stderr)
                self.assertNotIn("vendored OpenJPH fallback was entered", result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required for package discovery checks")
    def test_minimum_tracks_upstream_not_a_builder_allowlist(self):
        for version, success in (("0.39.0", False), ("0.42.1", True)):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                result, _ = self._configure_probe(Path(tmp), version, minimum="0.40.0")
                self.assertEqual(success, result.returncode == 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("cmake"), "CMake is required for package discovery checks")
    def test_exports_keep_package_abi_checks_for_the_selected_build_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result, build = self._configure_probe(root, "0.33.0")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            for version, success in (("0.33.1", True), ("0.34.0", False)):
                with self.subTest(version=version):
                    consumer = root / f"consumer-{version}"
                    consumer.mkdir()
                    (consumer / "CMakeLists.txt").write_text(
                        "cmake_minimum_required(VERSION 3.14)\nproject(consumer LANGUAGES NONE)\n"
                        "include(CMakePackageConfigHelpers)\ninclude(CMakeFindDependencyMacro)\n"
                        + _ISOLATED_FIND_SETTINGS
                        + f'set(CMAKE_PREFIX_PATH "{root.as_posix()}/package")\n'
                        + f'write_basic_package_version_file("{root.as_posix()}/package/openjph-config-version.cmake" '
                        + f'VERSION "{version}" COMPATIBILITY SameMinorVersion)\n'
                        + f'set(OpenEXR_DIR "{build.as_posix()}")\n'
                        + "find_package(OpenEXR CONFIG REQUIRED)\n",
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        ["cmake", "-S", str(consumer), "-B", str(consumer / "build")],
                        capture_output=True, text=True,
                    )
                    self.assertEqual(success, result.returncode == 0, result.stdout + result.stderr)
                    if not success:
                        self.assertIn("0.33.0", result.stderr)
                        self.assertIn("0.34.0", result.stderr)


if __name__ == "__main__":
    unittest.main()
