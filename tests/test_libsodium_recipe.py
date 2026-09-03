from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.config import load_config
from builder.license_policy import LICENSE_RECORDS
from builder.recipes import libsodium


def _builder(
    *,
    static: bool,
    os_name: str = "windows",
    dry_run: bool = True,
    runtime: str | None = None,
):
    windows = {
        "debug_postfix": "d",
        "msvc_runtime": runtime or ("static" if static else "dynamic"),
    }
    return SimpleNamespace(
        config=SimpleNamespace(
            global_cfg=SimpleNamespace(
                build_libsodium=True,
                static_default=static,
                windows=windows,
            )
        ),
        platform=SimpleNamespace(os=os_name, arch="x86_64"),
        toolchain={"cc": "cl.exe" if os_name == "windows" else "cc"},
        repo_paths={},
        dry_run=dry_run,
        _jobs=lambda: 8,
        _windows_runtime_mode=lambda: windows["msvc_runtime"],
    )


def _context(root: Path, build_type: str):
    return SimpleNamespace(
        repo=SimpleNamespace(name="libsodium"),
        build_type=build_type,
        build_dir=root / "build",
        install_prefix=root / "prefix",
        src_dir=root / "source",
    )


class LibsodiumRecipeTests(unittest.TestCase):
    def test_build_config_registers_stable_optional_repo_and_toggle(self):
        project_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "build.toml"
            config_path.write_text(
                (project_root / "build.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (config_path.parent / "build.user.toml").write_text(
                "[global]\nbuild_libsodium = true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

        repo = next(repo for repo in config.repos if repo.name == "libsodium")
        self.assertEqual(repo.url, "https://github.com/jedisct1/libsodium.git")
        self.assertEqual(repo.ref, "stable")
        self.assertEqual(repo.ref_type, "branch")
        self.assertEqual(repo.build_system, "libsodium")
        self.assertTrue(repo.optional)
        self.assertTrue(config.global_cfg.build_libsodium)

    def test_recipe_is_optional_and_isc_is_allowed(self):
        builder = _builder(static=True)
        builder.config.global_cfg.build_libsodium = False
        self.assertFalse(libsodium.enabled(builder, None))
        builder.config.global_cfg.build_libsodium = True
        self.assertTrue(libsodium.enabled(builder, None))

        record = LICENSE_RECORDS["libsodium"]
        self.assertEqual(record.expression, "ISC")
        self.assertEqual(record.disposition, "allow")

    def test_build_system_uses_autotools_except_on_native_windows(self):
        for os_name in ("linux", "macos"):
            builder = _builder(static=True, os_name=os_name)
            self.assertEqual(libsodium.resolve_build_system(builder, None, Path()), "autotools")
        self.assertEqual(
            libsodium.resolve_build_system(_builder(static=True), None, Path()),
            "libsodium",
        )

    def test_windows_static_debug_uses_upstream_solution_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Debug")
            command = libsodium._windows_build_command(_builder(static=True), ctx, {})

        self.assertTrue(command[1].endswith("builds/msvc/vs2022/libsodium.sln"))
        self.assertIn("/property:Configuration=StaticDebug", command)
        self.assertIn("/property:Platform=x64", command)
        self.assertIn("/property:TargetName=libsodiumd", command)
        self.assertTrue(any(arg.startswith("/property:OutDir=") for arg in command))
        self.assertTrue(any(arg.startswith("/property:IntDir=") for arg in command))

    def test_windows_dynamic_release_uses_dll_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Release")
            command = libsodium._windows_build_command(
                _builder(static=False, runtime="static"), ctx, {}
            )

        self.assertIn("/property:Configuration=DynRelease", command)
        self.assertIn("/property:TargetName=libsodium", command)
        self.assertIn(
            f"/property:ForceImportBeforeCppTargets={ctx.build_dir / 'oiio-builder-msvc-runtime.props'}",
            command,
        )

    def test_windows_runtime_property_sheet_is_independent_of_linkage(self):
        static_dll = libsodium._windows_runtime_props_text(
            _builder(static=False, runtime="static"), "Debug"
        )
        dynamic_static_library = libsodium._windows_runtime_props_text(
            _builder(static=True, runtime="dynamic"), "Release"
        )

        self.assertIn("<RuntimeLibrary>MultiThreadedDebug</RuntimeLibrary>", static_dll)
        self.assertIn("<UndefinePreprocessorDefinitions>_DLL;", static_dll)
        self.assertIn(
            "<RuntimeLibrary>MultiThreadedDLL</RuntimeLibrary>",
            dynamic_static_library,
        )
        self.assertNotIn("UndefinePreprocessorDefinitions", dynamic_static_library)

    def test_windows_rejects_unsupported_toolchain_and_asan(self):
        static_builder = _builder(static=True)
        static_builder.toolchain["cc"] = "clang-cl.exe"
        with self.assertRaisesRegex(RuntimeError, "VS2022 MSVC project"):
            libsodium._validate_windows_toolchain(static_builder)

        with self.assertRaisesRegex(RuntimeError, "no ASAN configuration"):
            libsodium._windows_configuration(_builder(static=True), "ASAN")

    def test_windows_header_install_excludes_private_headers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _context(root, "Release")
            sodium_headers = ctx.src_dir / "src" / "libsodium" / "include" / "sodium"
            private_headers = sodium_headers / "private"
            private_headers.mkdir(parents=True)
            (ctx.src_dir / "src/libsodium/include/sodium.h").write_text(
                "public\n", encoding="utf-8"
            )
            (sodium_headers / "core.h").write_text("public\n", encoding="utf-8")
            (private_headers / "internal.h").write_text("private\n", encoding="utf-8")
            version = ctx.src_dir / "builds" / "msvc" / "version.h"
            version.parent.mkdir(parents=True)
            version.write_text(
                '#define SODIUM_VERSION_STRING "1.0.22"\n', encoding="utf-8"
            )

            libsodium._install_windows_headers(ctx)

            self.assertTrue((ctx.install_prefix / "include/sodium.h").is_file())
            self.assertTrue((ctx.install_prefix / "include/sodium/core.h").is_file())
            self.assertTrue((ctx.install_prefix / "include/sodium/version.h").is_file())
            self.assertFalse((ctx.install_prefix / "include/sodium/private/internal.h").exists())

    def test_generated_packages_expose_correct_link_contracts(self):
        windows_static = libsodium._cmake_package_text(_builder(static=True), "1.0.22")
        self.assertIn("add_library(sodium::sodium STATIC IMPORTED)", windows_static)
        self.assertIn("IMPORTED_LOCATION_DEBUG \"${_sodium_prefix}/lib/libsodiumd.lib\"", windows_static)
        self.assertIn("INTERFACE_COMPILE_DEFINITIONS \"SODIUM_STATIC\"", windows_static)
        self.assertIn("INTERFACE_LINK_LIBRARIES \"advapi32\"", windows_static)

        windows_shared = libsodium._cmake_package_text(_builder(static=False), "1.0.22")
        self.assertIn("add_library(sodium::sodium SHARED IMPORTED)", windows_shared)
        self.assertIn("IMPORTED_IMPLIB_RELEASE \"${_sodium_prefix}/lib/libsodium.lib\"", windows_shared)
        self.assertIn("IMPORTED_LOCATION_DEBUG \"${_sodium_prefix}/bin/libsodiumd.dll\"", windows_shared)
        self.assertNotIn("SODIUM_STATIC", windows_shared)

        linux_static = libsodium._cmake_package_text(
            _builder(static=True, os_name="linux"), "1.0.22"
        )
        self.assertIn("find_dependency(Threads REQUIRED)", linux_static)
        self.assertIn("IMPORTED_LOCATION \"${_sodium_prefix}/lib/libsodium.a\"", linux_static)

        macos_shared = libsodium._cmake_package_text(
            _builder(static=False, os_name="macos"), "1.0.22"
        )
        self.assertIn("IMPORTED_LOCATION \"${_sodium_prefix}/lib/libsodium.dylib\"", macos_shared)

    def test_generated_package_configures_in_an_independent_consumer(self):
        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("cmake is required for the package probe")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            package_dir = prefix / "lib" / "cmake" / "sodium"
            package_dir.mkdir(parents=True)
            (package_dir / "sodiumConfig.cmake").write_text(
                libsodium._cmake_package_text(
                    _builder(static=True, os_name="linux"), "1.0.22"
                ),
                encoding="utf-8",
            )
            (package_dir / "sodiumConfigVersion.cmake").write_text(
                libsodium._cmake_version_text("1.0.22"),
                encoding="utf-8",
            )

            source_dir = root / "consumer"
            source_dir.mkdir()
            (source_dir / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(libsodium_package_probe C)\n"
                "find_package(sodium 1.0.22 EXACT CONFIG REQUIRED)\n"
                "if(NOT TARGET sodium::sodium)\n"
                "  message(FATAL_ERROR \"missing sodium::sodium\")\n"
                "endif()\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    cmake,
                    "-S",
                    str(source_dir),
                    "-B",
                    str(root / "build"),
                    f"-DCMAKE_PREFIX_PATH={prefix}",
                ],
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
