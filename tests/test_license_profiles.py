from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder import backends
from builder.license_policy import (
    LGPL_DYNAMIC,
    NONGPL_STATIC,
    apply_profile_defaults,
    normalize_profile,
    profile_cmake_args,
    profile_manifest,
    rejected_reason,
    resolve_profile,
    validate_installed_artifacts,
)
from builder.core import Builder
from builder.recipes import (
    dng_sdk,
    ffmpeg,
    lcms2,
    minizip_ng,
    opencolorio,
    openexr,
    openimageio,
    openmeta,
    pystring,
)
from builder.vcpkg_import import find_triplet


def _profile_cfg(**overrides):
    values = {
        "profile": LGPL_DYNAMIC,
        "static_default": True,
        "build_x265": True,
        "build_qt6": False,
        "qt6_modules": ["qtbase"],
        "windows": {"msvc_runtime": "static"},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LicenseProfileTests(unittest.TestCase):
    def test_normalize_and_resolve_lgpl_dynamic(self):
        self.assertEqual(normalize_profile("LGPL"), LGPL_DYNAMIC)
        self.assertEqual(normalize_profile("lgpl_dynamic"), LGPL_DYNAMIC)
        profile = resolve_profile(LGPL_DYNAMIC)
        self.assertIsNotNone(profile)
        self.assertEqual(profile.linkage, "dynamic")

    def test_lgpl_dynamic_forces_linkage_and_rejects_gpl(self):
        cfg = _profile_cfg()
        apply_profile_defaults(cfg)
        self.assertFalse(cfg.static_default)
        self.assertFalse(cfg.build_x265)
        self.assertEqual(cfg.windows["msvc_runtime"], "dynamic")

        profile = resolve_profile(LGPL_DYNAMIC)
        self.assertIsNone(rejected_reason(profile, "libheif"))
        self.assertIsNone(rejected_reason(profile, "ffmpeg"))
        self.assertIsNotNone(rejected_reason(profile, "x265"))
        self.assertIsNotNone(rejected_reason(profile, "unreviewed-package"))

    def test_lgpl_dynamic_rejects_qt_module_sets_with_gpl_artifacts(self):
        cfg = _profile_cfg(build_qt6=True, qt6_modules=["qtbase", "qttools"])
        with self.assertRaisesRegex(ValueError, "qttools"):
            apply_profile_defaults(cfg)

    def test_dynamic_cmake_guards_are_final_and_package_specific(self):
        profile = resolve_profile(LGPL_DYNAMIC)
        heif_args = profile_cmake_args(profile, "libheif")
        self.assertIn("-DBUILD_SHARED_LIBS=ON", heif_args)
        self.assertIn("-DPKG_CONFIG_USE_STATIC_LIBS=OFF", heif_args)
        self.assertIn("-DWITH_X265=OFF", heif_args)

        oiio_args = profile_cmake_args(profile, "OpenImageIO")
        self.assertIn("-DBUILD_SHARED_LIBS=ON", oiio_args)
        self.assertIn("-DLINKSTATIC=OFF", oiio_args)

        png_args = profile_cmake_args(profile, "libpng")
        self.assertIn("-DPNG_SHARED=ON", png_args)
        self.assertIn("-DPNG_STATIC=OFF", png_args)

        zstd_args = profile_cmake_args(profile, "zstd")
        self.assertIn("-DZSTD_BUILD_SHARED=ON", zstd_args)
        self.assertIn("-DZSTD_BUILD_STATIC=OFF", zstd_args)
        self.assertIn("-DZSTD_PROGRAMS_LINK_SHARED=ON", zstd_args)

    def test_manifest_records_lgpl_selection_and_warnings(self):
        profile = resolve_profile(LGPL_DYNAMIC)
        self.assertIsNotNone(profile)
        manifest = profile_manifest(profile, ["ffmpeg", "libheif"], {"x265": "GPL"})
        selected = {entry["name"]: entry for entry in manifest["selected_repositories"]}
        self.assertEqual(selected["ffmpeg"]["disposition"], "lgpl")
        self.assertTrue(any("exact source" in warning for warning in manifest["warnings"]))

    def test_nongpl_static_behavior_is_preserved(self):
        profile = resolve_profile(NONGPL_STATIC)
        self.assertIsNotNone(rejected_reason(profile, "libheif"))
        self.assertIsNotNone(rejected_reason(profile, "x265"))
        self.assertIsNone(rejected_reason(profile, "OpenImageIO"))

    def test_lgpl_install_validation_requires_shared_and_rejects_static(self):
        profile = resolve_profile(LGPL_DYNAMIC)
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "lib").mkdir()
            with self.assertRaisesRegex(RuntimeError, "requires shared libheif"):
                validate_installed_artifacts(profile, "libheif", prefix, "linux")

            (prefix / "lib" / "libheif.so.1").touch()
            validate_installed_artifacts(profile, "libheif", prefix, "linux")

            (prefix / "lib" / "libheif.a").touch()
            with self.assertRaisesRegex(RuntimeError, "forbids static libheif"):
                validate_installed_artifacts(profile, "libheif", prefix, "linux")


class LinkageBackendTests(unittest.TestCase):
    def test_autotools_linkage_follows_prefix(self):
        static_builder = SimpleNamespace(config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=True)))
        shared_builder = SimpleNamespace(config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=False)))
        self.assertEqual(backends._autotools_linkage_args(static_builder), ["--disable-shared", "--enable-static"])
        self.assertEqual(backends._autotools_linkage_args(shared_builder), ["--enable-shared", "--disable-static"])

    def test_ffmpeg_lgpl_profile_is_shared_and_explicitly_guarded(self):
        cfg = SimpleNamespace(static_default=False, windows={})
        builder = SimpleNamespace(
            config=SimpleNamespace(global_cfg=cfg),
            platform=SimpleNamespace(os="linux", arch="x86_64"),
            license_profile=resolve_profile(LGPL_DYNAMIC),
            toolchain={},
            _non_cmake_flags=lambda _build_type: ("", "", ""),
        )
        ctx = SimpleNamespace(install_prefix=Path("/tmp/lgpl-prefix"), build_type="Release")
        args = ffmpeg._configure_args(builder, ctx)
        self.assertIn("--enable-shared", args)
        self.assertIn("--disable-static", args)
        self.assertIn("--disable-gpl", args)
        self.assertIn("--disable-nonfree", args)
        self.assertNotIn("--pkg-config-flags=--static", args)

    def test_lcms_builds_only_the_selected_variant(self):
        builder = SimpleNamespace(config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=False)))
        args = lcms2.cmake_args(builder, None)
        self.assertIn("-DLCMS2_BUILD_SHARED=ON", args)
        self.assertIn("-DLCMS2_BUILD_STATIC=OFF", args)

    def test_pystring_exports_symbols_only_for_windows_shared_builds(self):
        def builder(os_name: str, static_default: bool):
            return SimpleNamespace(
                platform=SimpleNamespace(os=os_name),
                config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=static_default)),
            )

        self.assertEqual(
            pystring.cmake_args(builder("windows", False), None),
            ["-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=ON"],
        )
        self.assertEqual(pystring.cmake_args(builder("windows", True), None), [])
        self.assertEqual(pystring.cmake_args(builder("linux", False), None), [])

    def test_minizip_exports_symbols_for_windows_shared_builds(self):
        def builder(os_name: str, static_default: bool):
            return SimpleNamespace(
                platform=SimpleNamespace(os=os_name),
                config=SimpleNamespace(
                    global_cfg=SimpleNamespace(
                        static_default=static_default,
                        windows={"debug_postfix": "d"},
                    )
                ),
            )

        shared_windows = builder("windows", False)
        self.assertEqual(
            minizip_ng.cmake_args(shared_windows, None),
            ["-DCMAKE_WINDOWS_EXPORT_ALL_SYMBOLS=ON"],
        )
        self.assertEqual(minizip_ng.cmake_args(builder("windows", True), None), [])
        self.assertEqual(minizip_ng.cmake_args(builder("linux", False), None), [])

        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "lib").mkdir()
            with self.assertRaisesRegex(RuntimeError, "missing its import library"):
                minizip_ng.post_install(shared_windows, prefix, "Debug")

            (prefix / "lib" / "minizip-ngd.lib").touch()
            minizip_ng.post_install(shared_windows, prefix, "Debug")

    def test_openmeta_builds_windows_wheel_only_for_release(self):
        cfg = SimpleNamespace(static_default=False, use_libcxx=False)
        builder = SimpleNamespace(
            config=SimpleNamespace(global_cfg=cfg),
            platform=SimpleNamespace(os="windows"),
            repos=[],
            _windows_python_wrappers_enabled=lambda: (True, "auto"),
        )

        debug_args = openmeta.cmake_args(builder, SimpleNamespace(build_type="Debug"))
        self.assertIn("-DOPENMETA_BUILD_PYTHON=ON", debug_args)
        self.assertIn("-DOPENMETA_BUILD_WHEEL=OFF", debug_args)

        release_args = openmeta.cmake_args(builder, SimpleNamespace(build_type="Release"))
        self.assertIn("-DOPENMETA_BUILD_PYTHON=ON", release_args)
        self.assertIn("-DOPENMETA_BUILD_WHEEL=ON", release_args)

    def test_opencolorio_uses_debug_python_extension_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            python_cmake = source_root / "src" / "bindings" / "python" / "CMakeLists.txt"
            python_cmake.parent.mkdir(parents=True)
            python_cmake.write_text(
                "if(WIN32)\n"
                "\t# Windows uses .pyd extension for python modules\n"
                "\tset_target_properties(PyOpenColorIO PROPERTIES\n"
                "\t\tSUFFIX \".pyd\"\n"
                "\t)\n"
                "endif()\n",
                encoding="utf-8",
            )
            cpu_file = (
                source_root
                / "src"
                / "OpenColorIO"
                / "ops"
                / "fixedfunction"
                / "FixedFunctionOpCPU.cpp"
            )
            cpu_file.parent.mkdir(parents=True)
            cpu_file.write_text(
                "#if (_MSC_VER >= 1920) && (OCIO_USE_AVX)\n",
                encoding="utf-8",
            )
            builder = SimpleNamespace(platform=SimpleNamespace(os="windows"))

            opencolorio.patch_source(builder, source_root)
            patched = python_cmake.read_text(encoding="utf-8")
            patched_cpu = cpu_file.read_text(encoding="utf-8")
            self.assertIn(opencolorio._PYTHON_SUFFIX_FIX_BEGIN, patched)
            self.assertIn("sysconfig.get_config_var('EXT_SUFFIX')", patched)
            self.assertIn('DEBUG_POSTFIX ""', patched)
            self.assertIn("!defined(__clang__)", patched_cpu)

            opencolorio.patch_source(builder, source_root)
            self.assertEqual(python_cmake.read_text(encoding="utf-8"), patched)
            self.assertEqual(cpu_file.read_text(encoding="utf-8"), patched_cpu)

            prefix = source_root / "prefix"
            package_dir = prefix / "lib" / "site-packages" / "PyOpenColorIO"
            package_dir.mkdir(parents=True)
            legacy_module = package_dir / "PyOpenColorIOd.pyd"
            abi_module = package_dir / "PyOpenColorIO_d.cp313-win_amd64.pyd"
            legacy_module.touch()
            abi_module.touch()
            builder.config = SimpleNamespace(
                global_cfg=SimpleNamespace(windows={"debug_postfix": "d"})
            )
            opencolorio.post_install(builder, prefix, "Debug")
            self.assertFalse(legacy_module.exists())
            self.assertTrue(abi_module.exists())

    def test_openexr_python_patch_uses_pybind_module_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            cmake_file = source_root / "src" / "wrappers" / "python" / "CMakeLists.txt"
            cmake_file.parent.mkdir(parents=True)
            cmake_file.write_text(
                'target_link_libraries (PyOpenEXR PRIVATE "${Python3_LIBRARIES}" '
                "OpenEXR::OpenEXR pybind11::headers)\n",
                encoding="utf-8",
            )
            keycode_header = source_root / "src" / "lib" / "OpenEXR" / "ImfKeyCode.h"
            keycode_header.parent.mkdir(parents=True)
            keycode_header.write_text(
                "class KeyCode {\npublic:\n    bool operator== (const KeyCode& other) const;\n};\n",
                encoding="utf-8",
            )
            builder = SimpleNamespace(platform=SimpleNamespace(os="windows"))

            openexr.patch_source(builder, source_root)
            patched = cmake_file.read_text(encoding="utf-8")
            patched_keycode = keycode_header.read_text(encoding="utf-8")
            self.assertIn("OpenEXR::OpenEXR pybind11::module", patched)
            self.assertNotIn("Python3_LIBRARIES", patched)
            self.assertNotIn("pybind11::headers", patched)
            self.assertIn(
                "    IMF_EXPORT\n    bool operator== (const KeyCode& other) const;",
                patched_keycode,
            )

            cmake_file.write_text(
                "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_BEGIN\n"
                "target_link_libraries (PyOpenEXR PRIVATE OpenEXR::OpenEXR pybind11::headers)\n"
                "if(TARGET Python3::Module)\n"
                "  target_link_libraries (PyOpenEXR PRIVATE Python3::Module)\n"
                "endif()\n"
                "# OIIO_BUILDER_PYOPENEXR_LINK_FIX_END\n",
                encoding="utf-8",
            )
            openexr.patch_source(builder, source_root)
            self.assertEqual(cmake_file.read_text(encoding="utf-8"), patched)
            self.assertEqual(keycode_header.read_text(encoding="utf-8"), patched_keycode)

            openexr.patch_source(builder, source_root)
            self.assertEqual(cmake_file.read_text(encoding="utf-8"), patched)
            self.assertEqual(keycode_header.read_text(encoding="utf-8"), patched_keycode)

    def test_openimageio_unsets_giflib_windows_compatibility_macros(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            gifinput = source_root / "src" / "gif.imageio" / "gifinput.cpp"
            gifinput.parent.mkdir(parents=True)
            gifinput.write_text(
                "#define reallocarray giflib_reallocarray_private_\n"
                "#include <gif_lib.h>\n"
                "#undef reallocarray\n"
                "\n"
                "#include <OpenImageIO/imageio.h>\n",
                encoding="utf-8",
            )

            openimageio._patch_giflib_windows_macro_leak(source_root)
            patched = gifinput.read_text(encoding="utf-8")
            self.assertLess(patched.index("#include <gif_lib.h>"), patched.index("#    undef open"))
            self.assertLess(patched.index("#    undef strtok_r"), patched.index("#include <OpenImageIO/imageio.h>"))
            for name in ("open", "close", "fdopen", "unlink", "strdup", "strtok_r"):
                self.assertIn(f"#    undef {name}", patched)

            openimageio._patch_giflib_windows_macro_leak(source_root)
            self.assertEqual(gifinput.read_text(encoding="utf-8"), patched)

    def test_dng_sdk_lcms2_compat_preserves_imported_target(self):
        old_target_block = """\
        if((_dng_lcms2_release OR _dng_lcms2_debug) AND NOT TARGET dng_sdk::lcms2)
            add_library(dng_sdk::lcms2 UNKNOWN IMPORTED)
            if(_dng_lcms2_release)
                set_target_properties(dng_sdk::lcms2 PROPERTIES
                    IMPORTED_LOCATION "${_dng_lcms2_release}"
                    IMPORTED_LOCATION_RELEASE "${_dng_lcms2_release}"
                    IMPORTED_LOCATION_MINSIZEREL "${_dng_lcms2_release}"
                    IMPORTED_LOCATION_RELWITHDEBINFO "${_dng_lcms2_release}"
                )
            endif()
            if(_dng_lcms2_debug)
                set_target_properties(dng_sdk::lcms2 PROPERTIES IMPORTED_LOCATION_DEBUG "${_dng_lcms2_debug}")
            endif()
            if(TARGET lcms2::lcms2)
                get_target_property(_dng_lcms2_iface_links lcms2::lcms2 INTERFACE_LINK_LIBRARIES)
                if(_dng_lcms2_iface_links)
                    set_target_properties(dng_sdk::lcms2 PROPERTIES INTERFACE_LINK_LIBRARIES "${_dng_lcms2_iface_links}")
                endif()
            endif()
        endif()
"""
        config_text = (
            "# OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_BEGIN\n"
            "# OIIO_BUILDER_LCMS2_LOCATION_FALLBACK_END\n"
            + old_target_block
        )

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "lib" / "cmake" / "dng_sdk" / "dng_sdk-config.cmake"
            config_path.parent.mkdir(parents=True)
            config_path.write_text(config_text, encoding="utf-8")

            Builder._ensure_dng_sdk_lcms2_compat(
                SimpleNamespace(dry_run=False), Path(tmp), "Debug"
            )
            patched = config_path.read_text(encoding="utf-8")

        self.assertIn(dng_sdk._LCMS2_TARGET_BRIDGE_BEGIN, patched)
        self.assertIn("add_library(dng_sdk::lcms2 INTERFACE IMPORTED)", patched)
        self.assertIn("INTERFACE_LINK_LIBRARIES lcms2::lcms2", patched)
        self.assertIn("elseif((_dng_lcms2_release OR _dng_lcms2_debug)", patched)

        self.assertEqual(dng_sdk._patch_lcms2_target_bridge(patched), patched)

        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("cmake is required for the imported-target probe")

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "src"
            build_dir = Path(tmp) / "build"
            source_dir.mkdir()
            (source_dir / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(dng_lcms_bridge NONE)\n"
                "add_library(lcms2::lcms2 SHARED IMPORTED)\n"
                "set_target_properties(lcms2::lcms2 PROPERTIES\n"
                '  IMPORTED_LOCATION_DEBUG "fake/lcms2d.dll"\n'
                '  IMPORTED_IMPLIB_DEBUG "fake/lcms2d.lib"\n'
                ")\n"
                'set(_dng_lcms2_release "fake/lcms2.dll")\n'
                'set(_dng_lcms2_debug "fake/lcms2d.dll")\n'
                + patched
                + "get_target_property(_bridge_type dng_sdk::lcms2 TYPE)\n"
                'if(NOT _bridge_type STREQUAL "INTERFACE_LIBRARY")\n'
                '  message(FATAL_ERROR "unexpected bridge type: ${_bridge_type}")\n'
                "endif()\n"
                "get_target_property(_bridge_links dng_sdk::lcms2 INTERFACE_LINK_LIBRARIES)\n"
                'if(NOT _bridge_links STREQUAL "lcms2::lcms2")\n'
                '  message(FATAL_ERROR "unexpected bridge links: ${_bridge_links}")\n'
                "endif()\n"
                "get_target_property(_bridge_location dng_sdk::lcms2 IMPORTED_LOCATION_DEBUG)\n"
                "if(_bridge_location)\n"
                '  message(FATAL_ERROR "bridge copied runtime DLL: ${_bridge_location}")\n'
                "endif()\n",
                encoding="utf-8",
            )
            subprocess.run(
                [cmake, "-S", str(source_dir), "-B", str(build_dir)],
                check=True,
                capture_output=True,
                text=True,
            )

    def test_vcpkg_triplet_preference_tracks_linkage(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp)
            dynamic = installed / "x64-windows"
            static = installed / "x64-windows-static"
            for triplet in (dynamic, static):
                (triplet / "include").mkdir(parents=True)
                (triplet / "include" / "marker.h").touch()
            (dynamic / "bin").mkdir()
            (dynamic / "bin" / "library.dll").touch()

            zip_path = Path(tmp) / "export.zip"
            self.assertEqual(
                find_triplet(installed, "include/marker.h", zip_path, prefer_static=False),
                dynamic,
            )
            self.assertEqual(
                find_triplet(installed, "include/marker.h", zip_path, prefer_static=True),
                static,
            )


if __name__ == "__main__":
    unittest.main()
