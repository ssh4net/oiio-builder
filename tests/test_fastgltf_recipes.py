from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.config import load_config
from builder.core import Builder
from builder.license_policy import rejected_reason, resolve_profile
from builder.platform import PlatformInfo
from builder.recipes import registry
from builder.repo_options import load_repo_defaults
from builder.topo import topo_sort


PROJECT_ROOT = Path(__file__).parents[1]
REPOS = {"fastgltf", "simdjson", "simdutf"}


class FastgltfRecipeTests(unittest.TestCase):
    def _config(self, **toggles):
        with tempfile.TemporaryDirectory() as tmp:
            user_config = Path(tmp) / "user.toml"
            user_config.write_text(
                "[global]\n" + "".join(
                    f"{name} = {'true' if value else 'false'}\n"
                    for name, value in toggles.items()
                ),
                encoding="utf-8",
            )
            return load_config(PROJECT_ROOT / "build.toml", user_config_path=user_config)

    def _selected(self, config, *, platform="linux"):
        builder = object.__new__(Builder)
        builder.config = config
        builder.platform = PlatformInfo(os=platform, arch="x86_64")
        builder.license_profile = resolve_profile(config.global_cfg.profile)
        return builder._filter_repos()

    def test_repositories_are_optional_and_disabled_by_default(self):
        config = load_config(PROJECT_ROOT / "build.toml", load_user_config=False)
        registered = {repo.name: repo for repo in config.repos if repo.name in REPOS}
        self.assertEqual(set(registered), REPOS)
        for name, repo in registered.items():
            with self.subTest(repo=name):
                self.assertTrue(repo.optional)
                self.assertEqual(repo.build_system, "cmake")
                self.assertEqual(repo.dir, name)
                self.assertTrue(repo.url.startswith("https://github.com/"))
                self.assertFalse(getattr(config.global_cfg, f"build_{name}"))
        self.assertFalse(REPOS.intersection(repo.name for repo in self._selected(config)))

    def test_fastgltf_alone_enables_and_orders_both_dependency_recipes(self):
        for platform in ("linux", "macos", "windows"):
            with self.subTest(platform=platform):
                config = self._config(build_fastgltf=True)
                config.only = {"fastgltf"}
                repos = self._selected(config, platform=platform)
                self.assertEqual({repo.name for repo in repos}, REPOS)
                self.assertFalse(config.global_cfg.build_simdjson)
                self.assertFalse(config.global_cfg.build_simdutf)
                order = topo_sort(
                    [repo.name for repo in repos],
                    {repo.name: repo.deps for repo in repos},
                    config.global_cfg.preferred_repo_order,
                )
                self.assertEqual(order[-1], "fastgltf")

    def test_dependency_recipes_can_be_selected_independently(self):
        for name in ("simdjson", "simdutf"):
            with self.subTest(repo=name):
                config = self._config(**{f"build_{name}": True})
                config.only = {name}
                self.assertEqual([repo.name for repo in self._selected(config)], [name])

    def test_all_user_toggles_are_accepted(self):
        config = self._config(**{f"build_{name}": True for name in REPOS})
        for name in REPOS:
            self.assertTrue(getattr(config.global_cfg, f"build_{name}"))

    def test_defaults_disable_bundling_and_preserve_prefix_linkage(self):
        defaults = load_repo_defaults(PROJECT_ROOT / "builder" / "recipes" / "defaults")
        fastgltf = defaults["fastgltf"].cmake.cache
        self.assertFalse(fastgltf["FASTGLTF_DOWNLOAD_SIMDJSON"])
        self.assertFalse(fastgltf["FASTGLTF_DOWNLOAD_SIMDUTF"])
        self.assertTrue(fastgltf["FASTGLTF_ENABLE_INSTALL"])
        for option in ("TESTS", "EXAMPLES", "DOCS", "GLTF_RS", "ASSIMP", "CPP_MODULES"):
            self.assertFalse(fastgltf[f"FASTGLTF_ENABLE_{option}"])
        simdjson = defaults["simdjson"].cmake.cache
        self.assertTrue(simdjson["SIMDJSON_INSTALL"])
        self.assertTrue(simdjson["SIMDJSON_SINGLEHEADER"])
        self.assertFalse(simdjson["SIMDJSON_DEVELOPER_MODE"])
        self.assertFalse(simdjson["SIMDJSON_BUILD_STATIC_LIB"])
        simdutf = defaults["simdutf"].cmake.cache
        for option in ("TESTS", "TOOLS", "BENCHMARKS", "FUZZERS", "ICONV"):
            self.assertFalse(simdutf[f"SIMDUTF_{option}"])
        for name in REPOS:
            cache = defaults[name].cmake.cache
            self.assertEqual(cache["CMAKE_INSTALL_LIBDIR"], "lib")
            self.assertNotIn("BUILD_SHARED_LIBS", cache)
            self.assertNotIn("CMAKE_MSVC_RUNTIME_LIBRARY", cache)

    def test_fastgltf_selects_dependency_packages_from_active_prefix(self):
        for platform in ("linux", "macos", "windows"):
            with self.subTest(platform=platform):
                builder = object.__new__(Builder)
                builder.platform = PlatformInfo(os=platform, arch="x86_64")
                prefix = Path("test prefix")
                ctx = SimpleNamespace(install_prefix=prefix)
                args = registry.cmake_args("fastgltf", builder, ctx)
                self.assertEqual(args, [
                    f"-Dsimdjson_DIR={builder._cmake_path_arg(prefix / 'lib/cmake/simdjson')}",
                    f"-Dsimdutf_DIR={builder._cmake_path_arg(prefix / 'lib/cmake/simdutf')}",
                ])

    def test_license_profiles_allow_the_complete_dependency_graph(self):
        for profile in ("nongpl-static", "lgpl-dynamic"):
            config = self._config(build_fastgltf=True)
            config.global_cfg.profile = profile
            config.only = {"fastgltf"}
            self.assertEqual({repo.name for repo in self._selected(config)}, REPOS)
            for name in REPOS:
                self.assertIsNone(rejected_reason(resolve_profile(profile), name))


if __name__ == "__main__":
    unittest.main()
