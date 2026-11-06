from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from builder.config import load_config
from builder.core import Builder
from builder.license_policy import rejected_reason, resolve_profile
from builder.platform import PlatformInfo
from builder.recipes import oidn, registry
from builder.repo_options import load_repo_defaults

ROOT = Path(__file__).parents[1]


class OidnRecipeTests(unittest.TestCase):
    def test_disabled_by_default_and_enabled_in_user_configs(self):
        config = load_config(ROOT / "build.toml", load_user_config=False)
        self.assertFalse(config.global_cfg.build_oidn)
        repo = next(repo for repo in config.repos if repo.name == "oidn")
        self.assertTrue(repo.optional)
        self.assertEqual((repo.ref, repo.ref_type), ("v2.5.1", "tag"))
        for os_name in ("linux", "macos", "windows"):
            builder = object.__new__(Builder)
            builder.config = config
            builder.platform = PlatformInfo(os=os_name, arch="x86_64")
            builder.license_profile = None
            self.assertFalse(registry.enabled("oidn", builder, repo))
            config.global_cfg.build_oidn = True
            config.only = {"oidn"}
            self.assertEqual([r.name for r in builder._filter_repos()], ["oidn"])
            config.global_cfg.build_oidn = False
        for name in ("build_mac.user.toml", "build_win.user.toml", "build_ub_ngpl.user.toml"):
            path = ROOT / name
            # These files are intentionally machine-local and may be absent in CI.
            if path.is_file():
                self.assertTrue(load_config(ROOT / "build.toml", user_config_path=path).global_cfg.build_oidn)

    def test_platform_defaults_and_prefix_linkage(self):
        defaults = load_repo_defaults(ROOT / "builder/recipes/defaults")["oidn"]
        for os_name in ("linux", "windows", "macos"):
            cache = defaults.resolve(os_name).cache
            self.assertFalse(cache["OIDN_DEVICE_CPU"])
            self.assertEqual(cache["OIDN_DEVICE_CUDA"], os_name != "macos")
            self.assertEqual(cache["OIDN_DEVICE_METAL"], os_name == "macos")
            self.assertTrue(cache["OIDN_FILTER_RTLIGHTMAP"])
            self.assertTrue(cache["OIDN_APPS"])
            self.assertEqual(cache["OIDN_DEVICE_CUDA_API"], "RuntimeStatic")
        for static in (True, False):
            builder = SimpleNamespace(config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=static)),
                                      platform=PlatformInfo(os="linux", arch="x86_64"))
            args = registry.cmake_args("oidn", builder, None)
            value = "ON" if static else "OFF"
            self.assertIn(f"-DOIDN_STATIC_LIB={value}", args)
            self.assertIn(f"-DOIDN_BUILDER_STATIC_CUDA={value}", args)
        for profile in ("nongpl-static", "lgpl-dynamic"):
            self.assertIsNone(rejected_reason(resolve_profile(profile), "oidn"))

    def test_metal_defaults_apply_only_to_apple_silicon(self):
        for arch in ("arm64", "x86_64"):
            builder = SimpleNamespace(config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=True)),
                                      platform=PlatformInfo(os="macos", arch=arch))
            args = oidn.cmake_args(builder, None)
            self.assertEqual("-DOIDN_DEVICE_METAL=OFF" in args, arch == "x86_64")
            self.assertEqual("-DOIDN_DEVICE_CPU=ON" in args, arch == "x86_64")

    def _patch_fixture(self, root: Path, newline: str = "\n") -> dict[Path, bytes]:
        original = {}
        for relative, edits in oidn._EDITS.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            data = ("\n---\n".join(old for old, _new in edits) + "\n").replace("\n", newline).encode()
            path.write_bytes(data)
            original[path] = data
        return original

    def test_patch_is_idempotent_preserves_crlf_and_dry_run_is_read_only(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original = self._patch_fixture(root, newline)
                oidn.patch_source(SimpleNamespace(dry_run=True), root)
                self.assertEqual(original, {p: p.read_bytes() for p in original})
                oidn.patch_source(SimpleNamespace(dry_run=False), root)
                updated = {p: p.read_bytes() for p in original}
                self.assertNotEqual(original, updated)
                oidn.patch_source(SimpleNamespace(dry_run=False), root)
                self.assertEqual(updated, {p: p.read_bytes() for p in original})
                if newline == "\r\n":
                    for data in updated.values():
                        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_source_drift_fails_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self._patch_fixture(root)
            path = root / "cmake/Config.cmake.in"
            path.write_text("changed upstream\n")
            original[path] = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "no longer matches"):
                oidn.patch_source(SimpleNamespace(dry_run=False), root)
            self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_lfs_pointers_fail_with_recovery_command_without_resetting_submodules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weights = root / "weights"
            weights.mkdir()
            (weights / "rt_hdr.tza").write_text("version https://git-lfs.github.com/spec/v1\n")
            builder = SimpleNamespace(dry_run=False,
                _repo_cmake_defaults_args=Mock(return_value=["-DOIDN_FILTER_RT=ON"]),
                _repo_cmake_user_override_args=Mock(return_value=[]))
            with self.assertRaisesRegex(RuntimeError, "lfs pull"):
                oidn.pre_build(builder, SimpleNamespace(cmake_args=[]), SimpleNamespace(src_dir=root), {})

    def test_source_preparation_follows_last_cmake_argument_and_preserves_populated_modules(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(oidn, "run") as run:
            root = Path(tmp)
            builder = SimpleNamespace(dry_run=False,
                _repo_cmake_defaults_args=Mock(return_value=["-DOIDN_FILTER_RT=ON", "-DOIDN_DEVICE_CUDA=ON"]),
                _repo_cmake_user_override_args=Mock(return_value=["-DOIDN_FILTER_RT=OFF", "-DOIDN_DEVICE_CUDA:BOOL=OFF"]))
            repo = SimpleNamespace(cmake_args=["-DOIDN_FILTER_RT=ON"])
            oidn.pre_build(builder, repo, SimpleNamespace(src_dir=root), {})
            run.assert_not_called()
            builder._repo_cmake_user_override_args.return_value = []
            for relative in ("weights/rt_hdr.tza", "external/cutlass/include/cutlass/cutlass.h"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"real source data")
            oidn.pre_build(builder, repo, SimpleNamespace(src_dir=root), {})
            run.assert_not_called()

    def test_dry_run_only_reports_missing_required_submodules(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(oidn, "run") as run:
            root = Path(tmp)
            builder = SimpleNamespace(dry_run=True,
                _repo_cmake_defaults_args=Mock(return_value=["-DOIDN_FILTER_RT=ON"]),
                _repo_cmake_user_override_args=Mock(return_value=[]))
            oidn.pre_build(builder, SimpleNamespace(cmake_args=[]), SimpleNamespace(src_dir=root), {})
            run.assert_called_once_with(["git", "-C", str(root), "submodule", "update", "--init", "--", "weights"],
                                        env={}, dry_run=True)
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
