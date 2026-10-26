from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.config import load_config
from builder.core import Builder
from builder.license_policy import profile_cmake_args, resolve_profile
from builder.platform import PlatformInfo
from builder.recipes import geogram, geometry_central, registry
from builder.recipes._geometry_support import initialize_missing_submodules
from builder.repo_options import load_repo_defaults
from builder.topo import topo_sort

ROOT = Path(__file__).parents[1]


class GeometryRecipeTests(unittest.TestCase):
    def _config(self, **options):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.toml"
            user.write_text("[global]\n" + "".join(
                f"{key} = {'true' if value else 'false'}\n" for key, value in options.items()
            ), encoding="utf-8")
            return load_config(ROOT / "build.toml", user_config_path=user)

    def _selected(self, config, platform="linux"):
        builder = object.__new__(Builder)
        builder.config = config
        builder.platform = PlatformInfo(os=platform, arch="x86_64")
        builder.license_profile = resolve_profile(config.global_cfg.profile)
        return builder._filter_repos()

    def test_both_repositories_are_optional_and_disabled_by_default(self):
        config = load_config(ROOT / "build.toml", load_user_config=False)
        repos = {repo.name: repo for repo in config.repos}
        for name in ("geogram", "geometry-central"):
            self.assertTrue(repos[name].optional)
            self.assertEqual("cmake", repos[name].build_system)
            self.assertEqual(name, repos[name].dir)
            self.assertTrue(repos[name].url.startswith("https://github.com/"))
            self.assertNotIn(name, {repo.name for repo in self._selected(config)})
        self.assertFalse(config.global_cfg.build_geogram)
        self.assertFalse(config.global_cfg.build_geometry_central)

    def test_optional_libraries_enable_and_order_dependencies_even_with_groups_off(self):
        for platform in ("linux", "macos", "windows"):
            for name, toggle, dependency in (("geogram", "build_geogram", "glfw"),
                                              ("geometry-central", "build_geometry_central", "eigen")):
                with self.subTest(platform=platform, name=name):
                    config = self._config(**{toggle: True, "build_gl_stack": False, "build_imageio_stack": False})
                    config.only = {name}
                    repos = self._selected(config, platform)
                    self.assertEqual({name, dependency}, {repo.name for repo in repos})
                    order = topo_sort([repo.name for repo in repos], {repo.name: repo.deps for repo in repos}, [])
                    self.assertEqual([dependency, name], order)

    def test_both_license_profiles_allow_dependency_graph_and_disable_restricted_features(self):
        for profile in ("nongpl-static", "lgpl-dynamic"):
            config = self._config(build_geogram=True, build_geometry_central=True)
            config.global_cfg.profile = profile
            config.only = {"geogram", "geometry-central"}
            self.assertEqual({"geogram", "geometry-central", "glfw", "eigen"},
                             {repo.name for repo in self._selected(config)})
            args = profile_cmake_args(resolve_profile(profile), "geogram")
            self.assertIn("-DGEOGRAM_WITH_TETGEN=OFF", args)
            self.assertIn("-DGEOGRAM_WITH_TRIANGLE=OFF", args)
            if profile == "lgpl-dynamic":
                self.assertIn("-DBUILD_SHARED_LIBS=ON", args)

    def test_defaults_match_existing_features_without_hardcoding_linkage_or_machine_paths(self):
        defaults = load_repo_defaults(ROOT / "builder" / "recipes" / "defaults")
        geo = defaults["geogram"].cmake.cache
        for name in ("LIB_ONLY", "WITH_GRAPHICS", "USE_SYSTEM_GLFW3", "WITH_LUA", "WITH_LEGACY_NUMERICS",
                     "WITH_HLBFGS", "WITH_TETGEN", "WITH_TRIANGLE"):
            self.assertTrue(geo[f"GEOGRAM_{name}"])
        self.assertFalse(geo["GEOGRAM_WITH_TBB"])
        gc = defaults["geometry-central"].cmake.cache
        self.assertFalse(gc["SUITESPARSE"])
        self.assertFalse(gc["GC_ALWAYS_DOWNLOAD_EIGEN"])
        for cache in (geo, gc):
            self.assertEqual("lib", cache["CMAKE_INSTALL_LIBDIR"])
            self.assertNotIn("BUILD_SHARED_LIBS", cache)
            self.assertNotIn("CMAKE_MSVC_RUNTIME_LIBRARY", cache)

    def test_geogram_platform_mapping_respects_linkage_and_architecture(self):
        cases = (("windows", "x86_64", "cl.exe", "Win-vs-generic", "Win-vs-dynamic-generic"),
                 ("linux", "x86_64", "clang++-20", "Linux64-clang", "Linux64-clang-dynamic"),
                 ("linux", "x86_64", "g++", "Linux64-gcc", "Linux64-gcc-dynamic"),
                 ("linux", "arm64", "clang++", "Linux64-nonx86-clang-dynamic", "Linux64-nonx86-clang-dynamic"),
                 ("macos", "arm64", "clang++", "Darwin-aarch64-clang", "Darwin-aarch64-clang-dynamic"))
        for platform, arch, compiler, static_platform, shared_platform in cases:
            for shared, expected in ((False, static_platform), (True, shared_platform)):
                with self.subTest(platform=platform, arch=arch, shared=shared):
                    builder = SimpleNamespace(platform=PlatformInfo(os=platform, arch=arch),
                                              toolchain={"cxx": compiler},
                                              config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=not shared)))
                    args = registry.cmake_args("geogram", builder, SimpleNamespace(repo=SimpleNamespace(shared=None)))
                    self.assertIn(f"-DVORPALINE_PLATFORM={expected}", args)
                    self.assertIn(f"-DVORPALINE_BUILD_DYNAMIC={'ON' if shared else 'OFF'}", args)

    def _source_fixture(self, root: Path, name: str, newline: str):
        (root / "cmake").mkdir()
        if name == "geogram":
            root_text = "# user prefix\nif(GEOGRAM_WITH_TBB AND NOT TARGET TBB::tbb)\nendif()\n# user suffix\n"
            other = root / "cmake" / "geogram.cmake"
            other_text = "# Static versus dynamic builds\nif(VORPALINE_BUILD_DYNAMIC)\nendif()\n"
        else:
            root_text = "# user prefix\nadd_subdirectory(deps)\nadd_subdirectory(src)\n# user suffix\n"
            (root / "deps").mkdir()
            other = root / "deps" / "CMakeLists.txt"
            other_text = 'file(CREATE_LINK "a" "nanoflann/nanoflann.hpp" SYMBOLIC)\n'
        (root / "CMakeLists.txt").write_bytes(root_text.replace("\n", newline).encode())
        other.write_bytes(other_text.replace("\n", newline).encode())
        return [root / "CMakeLists.txt", other]

    def test_source_patches_are_idempotent_preserve_user_text_and_line_endings(self):
        for name, recipe in (("geogram", geogram), ("geometry-central", geometry_central)):
            for newline in ("\n", "\r\n"):
                with self.subTest(name=name, newline=newline), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    paths = self._source_fixture(root, name, newline)
                    recipe.patch_source(SimpleNamespace(dry_run=False), root)
                    before = {path: path.read_bytes() for path in paths}
                    recipe.patch_source(SimpleNamespace(dry_run=False), root)
                    self.assertEqual(before, {path: path.read_bytes() for path in paths})
                    self.assertIn(b"# user prefix", before[paths[0]])
                    self.assertIn(b"# user suffix", before[paths[0]])
                    self.assertIn(b"install(EXPORT", before[paths[0]])
                    if name == "geometry-central":
                        self.assertIn(b"find_package(Eigen3 3.3 CONFIG REQUIRED)", before[paths[0]])
                        self.assertIn(b"SYMBOLIC COPY_ON_ERROR", before[paths[1]])
                    if newline == "\r\n":
                        for contents in before.values():
                            self.assertNotIn(b"\n", contents.replace(b"\r\n", b""))

    def test_dry_run_does_not_patch_sources(self):
        for name, recipe in (("geogram", geogram), ("geometry-central", geometry_central)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                paths = self._source_fixture(root, name, "\n")
                before = {path: path.read_bytes() for path in paths}
                recipe.patch_source(SimpleNamespace(dry_run=True), root)
                self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_submodule_initialization_only_targets_missing_checkouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitmodules").write_text(
                '[submodule "existing"]\npath = deps/existing\n[submodule "absent"]\npath = deps/absent\n',
                encoding="utf-8",
            )
            (root / "deps" / "existing").mkdir(parents=True)
            (root / "deps" / "existing" / "user-file.h").write_text("preserve", encoding="utf-8")
            ctx = SimpleNamespace(src_dir=root, repo=SimpleNamespace(name="geometry-central"), build_type="Debug")
            builder = SimpleNamespace(dry_run=False, _repo_log_path=lambda *args: root / "submodules.log")
            with patch("builder.recipes._geometry_support.run") as run:
                initialize_missing_submodules(builder, ctx, {})
                command = run.call_args.args[0]
                self.assertEqual(["deps/absent"], command[command.index("--") + 1:])
            self.assertEqual("preserve", (root / "deps/existing/user-file.h").read_text())


if __name__ == "__main__":
    unittest.main()
