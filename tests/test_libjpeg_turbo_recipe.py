from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from builder.config import load_config
from builder.recipes import libjpeg_turbo
from builder.repo_options import load_repo_defaults


class LibjpegTurboRecipeTests(unittest.TestCase):
    @staticmethod
    def _write_cmake_lists(source_root: Path) -> Path:
        cmake_lists = source_root / "CMakeLists.txt"
        cmake_lists.write_text(
            "if(ENABLE_SHARED)\n"
            "  add_subdirectory(sharedlib)\n"
            "endif()\n\n"
            "if(ENABLE_STATIC)\n"
            "  add_library(jpeg-static STATIC jpeg.c)\n"
            "endif()\n",
            encoding="utf-8",
        )
        return cmake_lists

    def test_repo_depends_on_prefix_zlib_ng(self):
        project_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "build.toml"
            config_path.write_text(
                (project_root / "build.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            config = load_config(config_path)

        repo = next(repo for repo in config.repos if repo.name == "libjpeg-turbo")
        self.assertIn("zlib-ng", repo.deps)

    def test_defaults_use_prefix_zlib(self):
        defaults_dir = Path(__file__).parents[1] / "builder" / "recipes" / "defaults"
        options = load_repo_defaults(defaults_dir)["libjpeg-turbo"].cmake.cache
        self.assertTrue(options["WITH_SYSTEM_ZLIB"])

    def test_package_config_finds_exported_zlib_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            self._write_cmake_lists(source_root)
            config_template = source_root / "release" / "Config.cmake.in"
            config_template.parent.mkdir(parents=True)
            config_template.write_text(
                "@PACKAGE_INIT@\n\n"
                'include("${CMAKE_CURRENT_LIST_DIR}/@CMAKE_PROJECT_NAME@Targets.cmake")\n'
                'check_required_components("@CMAKE_PROJECT_NAME@")\n',
                encoding="utf-8",
            )

            libjpeg_turbo.patch_source(None, source_root)
            patched = config_template.read_text(encoding="utf-8")

            self.assertIn('if("@WITH_SYSTEM_ZLIB@" OR "@WITH_SYSTEM_SPNG@")', patched)
            self.assertIn("include(CMakeFindDependencyMacro)", patched)
            self.assertIn("find_dependency(ZLIB)", patched)
            self.assertLess(
                patched.index("find_dependency(ZLIB)"),
                patched.index("@CMAKE_PROJECT_NAME@Targets.cmake"),
            )

            libjpeg_turbo.patch_source(None, source_root)
            self.assertEqual(config_template.read_text(encoding="utf-8"), patched)

    def test_system_zlib_propagates_to_bundled_spng_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            cmake_lists = self._write_cmake_lists(source_root)
            config_template = source_root / "release" / "Config.cmake.in"
            config_template.parent.mkdir(parents=True)
            config_template.write_text(
                "@PACKAGE_INIT@\n\n"
                'include("${CMAKE_CURRENT_LIST_DIR}/@CMAKE_PROJECT_NAME@Targets.cmake")\n',
                encoding="utf-8",
            )

            libjpeg_turbo.patch_source(None, source_root)
            patched = cmake_lists.read_text(encoding="utf-8")

            self.assertIn("if(WITH_SYSTEM_ZLIB)", patched)
            self.assertIn(
                "target_link_libraries(spng-static PRIVATE ZLIB::ZLIB)",
                patched,
            )
            self.assertIn("target_link_libraries(spng PRIVATE ZLIB::ZLIB)", patched)
            self.assertLess(
                patched.index("target_link_libraries(spng-static PRIVATE ZLIB::ZLIB)"),
                patched.index("if(ENABLE_STATIC)"),
            )

            libjpeg_turbo.patch_source(None, source_root)
            self.assertEqual(cmake_lists.read_text(encoding="utf-8"), patched)

    def test_upstream_zlib_dependency_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            self._write_cmake_lists(source_root)
            config_template = source_root / "release" / "Config.cmake.in"
            config_template.parent.mkdir(parents=True)
            original = (
                "@PACKAGE_INIT@\n\n"
                "include(CMakeFindDependencyMacro)\n"
                "find_dependency(ZLIB)\n"
                'include("${CMAKE_CURRENT_LIST_DIR}/@CMAKE_PROJECT_NAME@Targets.cmake")\n'
            )
            config_template.write_text(original, encoding="utf-8")

            libjpeg_turbo.patch_source(None, source_root)

            self.assertEqual(config_template.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
