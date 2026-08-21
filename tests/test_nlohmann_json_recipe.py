from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.config import load_config
from builder.license_policy import LICENSE_RECORDS
from builder.recipes import nlohmann_json
from builder.repo_options import load_repo_defaults


class NlohmannJsonRecipeTests(unittest.TestCase):
    def test_build_config_registers_optional_repo_and_user_toggle(self):
        project_root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "build.toml"
            config_path.write_text(
                (project_root / "build.toml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (config_path.parent / "build.user.toml").write_text(
                "[global]\nbuild_nlohmann_json = true\n",
                encoding="utf-8",
            )
            config = load_config(config_path)

        repo = next(repo for repo in config.repos if repo.name == "nlohmann-json")
        self.assertEqual(repo.dir, "json")
        self.assertEqual(repo.build_system, "cmake")
        self.assertTrue(repo.optional)
        self.assertTrue(config.global_cfg.build_nlohmann_json)

    def test_recipe_is_disabled_by_default(self):
        builder = SimpleNamespace(
            config=SimpleNamespace(global_cfg=SimpleNamespace(build_nlohmann_json=False))
        )
        self.assertFalse(nlohmann_json.enabled(builder, None))

    def test_recipe_can_be_enabled(self):
        builder = SimpleNamespace(
            config=SimpleNamespace(global_cfg=SimpleNamespace(build_nlohmann_json=True))
        )
        self.assertTrue(nlohmann_json.enabled(builder, None))

    def test_defaults_install_the_header_only_cmake_package(self):
        defaults_dir = Path(__file__).parents[1] / "builder" / "recipes" / "defaults"
        options = load_repo_defaults(defaults_dir)["nlohmann-json"].cmake.cache
        self.assertFalse(options["BUILD_TESTING"])
        self.assertFalse(options["JSON_BuildTests"])
        self.assertTrue(options["JSON_Install"])
        self.assertTrue(options["JSON_MultipleHeaders"])
        self.assertFalse(options["NLOHMANN_JSON_BUILD_MODULES"])

    def test_license_profile_allows_the_mit_package(self):
        record = LICENSE_RECORDS["nlohmann-json"]
        self.assertEqual(record.expression, "MIT")
        self.assertEqual(record.disposition, "allow")


if __name__ == "__main__":
    unittest.main()
