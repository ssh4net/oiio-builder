from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.config import load_config
from builder.license_policy import LICENSE_RECORDS
from builder.recipes import sdl3
from builder.repo_options import CMakeOptions, load_repo_defaults


def _builder(*, os_name: str = "linux", static: bool = True, build_root: Path | None = None, cache: dict | None = None):
    return SimpleNamespace(
        config=SimpleNamespace(
            global_cfg=SimpleNamespace(build_sdl3=True, static_default=static, build_root=build_root or Path("."))
        ),
        platform=SimpleNamespace(os=os_name),
        dry_run=False,
        _repo_cmake_effective_toml_options=lambda _name: CMakeOptions(cache=dict(cache or {})),
    )


def _write_build_config(build_root: Path, build_type: str, text: str) -> None:
    header = build_root / build_type / "SDL3" / f"include-config-{build_type.lower()}" / "build_config" / "SDL_build_config.h"
    header.parent.mkdir(parents=True)
    header.write_text(text, encoding="utf-8")


class Sdl3RecipeTests(unittest.TestCase):
    def test_build_config_registers_optional_repo_and_user_toggle(self):
        project_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "build.toml"
            config_path.write_text((project_root / "build.toml").read_text(encoding="utf-8"), encoding="utf-8")
            (config_path.parent / "build.user.toml").write_text("[global]\nbuild_sdl3 = true\n", encoding="utf-8")
            config = load_config(config_path)
            default_config = load_config(config_path, load_user_config=False)

        repo = next(repo for repo in config.repos if repo.name == "SDL3")
        self.assertEqual(repo.dir, "SDL")
        self.assertEqual(repo.url, "https://github.com/libsdl-org/SDL.git")
        self.assertEqual((repo.ref, repo.ref_type), ("release-3.4.x", "branch"))
        self.assertEqual(repo.build_system, "cmake")
        self.assertTrue(repo.optional)
        self.assertTrue(config.global_cfg.build_sdl3)
        self.assertFalse(default_config.global_cfg.build_sdl3)

    def test_recipe_follows_the_toggle(self):
        builder = _builder()
        self.assertTrue(sdl3.enabled(builder, None))
        builder.config.global_cfg.build_sdl3 = False
        self.assertFalse(sdl3.enabled(builder, None))

    def test_builds_only_the_prefix_linkage(self):
        self.assertEqual(sdl3.cmake_args(_builder(static=True), None), ["-DSDL_SHARED=OFF", "-DSDL_STATIC=ON"])
        self.assertEqual(sdl3.cmake_args(_builder(static=False), None), ["-DSDL_SHARED=ON", "-DSDL_STATIC=OFF"])

    def test_windows_package_is_installed_under_lib_cmake(self):
        args = sdl3.cmake_args(_builder(os_name="windows"), None)
        self.assertIn("-DSDL_INSTALL_CMAKEDIR_ROOT=lib/cmake/SDL3", args)

    def test_defaults_skip_tests_examples_and_test_library(self):
        defaults_dir = Path(__file__).parents[1] / "builder" / "recipes" / "defaults"
        options = load_repo_defaults(defaults_dir)["SDL3"].cmake.cache
        self.assertFalse(options["SDL_TEST_LIBRARY"])
        self.assertFalse(options["SDL_TESTS"])
        self.assertFalse(options["SDL_EXAMPLES"])

    def test_license_profile_allows_the_zlib_library(self):
        record = LICENSE_RECORDS["SDL3"]
        self.assertEqual(record.expression, "Zlib")
        self.assertEqual(record.disposition, "allow")

    def test_linux_build_without_window_system_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_root = Path(tmp)
            _write_build_config(build_root, "Release", "#define SDL_VIDEO_DRIVER_OFFSCREEN 1\n/* #undef SDL_VIDEO_DRIVER_X11 */\n")
            with self.assertRaisesRegex(RuntimeError, "neither the X11 nor the Wayland"):
                sdl3.post_install(_builder(build_root=build_root), build_root / "prefix", "Release")

    def test_linux_build_with_a_window_system_passes(self):
        for driver in ("X11", "WAYLAND"):
            with self.subTest(driver=driver), tempfile.TemporaryDirectory() as tmp:
                build_root = Path(tmp)
                _write_build_config(build_root, "Debug", f"#define SDL_VIDEO_DRIVER_{driver} 1\n")
                sdl3.post_install(_builder(build_root=build_root), build_root / "prefix", "Debug")

    def test_window_system_check_respects_explicit_overrides(self):
        for cache in ({"SDL_VIDEO": False}, {"SDL_X11": "OFF", "SDL_WAYLAND": False}):
            with self.subTest(cache=cache), tempfile.TemporaryDirectory() as tmp:
                build_root = Path(tmp)
                _write_build_config(build_root, "Release", "#define SDL_VIDEO_DRIVER_OFFSCREEN 1\n")
                builder = _builder(build_root=build_root, cache=cache)
                self.assertFalse(sdl3.linux_window_system_required(builder))
                sdl3.post_install(builder, build_root / "prefix", "Release")
        self.assertTrue(sdl3.linux_window_system_required(_builder(cache={"SDL_X11": False})))

    def test_window_system_check_is_linux_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            build_root = Path(tmp)
            _write_build_config(build_root, "Release", "#define SDL_VIDEO_DRIVER_COCOA 1\n")
            sdl3.post_install(_builder(os_name="macos", build_root=build_root), build_root / "prefix", "Release")


if __name__ == "__main__":
    unittest.main()
