from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder import backends
from builder.config import load_config
from builder.core import Builder
from builder.license_policy import resolve_profile
from builder.platform import PlatformInfo
from builder.repo_options import load_repo_defaults, load_user_overrides

ROOT = Path(__file__).parents[1]
ADAPTER = "OPENMETA_WITH_DNG_SDK_ADAPTER"


class OpenMetaRecipeTests(unittest.TestCase):
    def _builder(self, user_path: Path, platform: str) -> Builder:
        config = load_config(ROOT / "build.toml", user_config_path=user_path)
        config.only = {"OpenMeta"}
        config.global_cfg.windows["python_wrappers"] = "on"
        builder = object.__new__(Builder)
        builder.config = config
        builder.platform = PlatformInfo(os=platform, arch="x86_64")
        builder.license_profile = resolve_profile(config.global_cfg.profile)
        builder._windows_python_wrappers_forced_on_note_printed = True
        builder.repos = builder._filter_repos()
        builder._repo_cmake_defaults = load_repo_defaults(ROOT / "builder/recipes/defaults")
        builder._repo_cmake_user_overrides = load_user_overrides(user_path)
        return builder

    def _adapter_values(self, builder: Builder, build_type: str) -> list[str]:
        repo = next(repo for repo in builder.repos if repo.name == "OpenMeta")
        args = backends._repo_specific_args(builder, repo, SimpleNamespace(build_type=build_type))
        args += repo.cmake_args + builder._repo_cmake_user_override_args(repo.name)
        return [arg.split("=", 1)[1] for arg in args if arg.startswith(f"-D{ADAPTER}=")]

    def test_adapter_stays_off_when_sdk_is_selected_for_other_consumers(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.toml"
            for sdk in (False, True):
                user.write_text(f"[global]\nbuild_dng_sdk = {'true' if sdk else 'false'}\n")
                for platform in ("linux", "windows", "macos"):
                    builder = self._builder(user, platform)
                    self.assertEqual(any(repo.name == "dng-sdk" for repo in builder.repos), sdk)
                    for build_type in ("Debug", "Release"):
                        with self.subTest(sdk=sdk, platform=platform, build_type=build_type):
                            self.assertEqual(self._adapter_values(builder, build_type), ["OFF"])

    def test_user_config_can_explicitly_enable_the_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "user.toml"
            user.write_text(
                '[global]\nbuild_dng_sdk = true\n\n[[repo_overrides]]\nname = "OpenMeta"\n'
                f'[repo_overrides.cmake.cache]\n{ADAPTER} = true\n'
            )
            for platform in ("linux", "windows", "macos"):
                builder = self._builder(user, platform)
                for build_type in ("Debug", "Release"):
                    with self.subTest(platform=platform, build_type=build_type):
                        self.assertEqual(self._adapter_values(builder, build_type), ["OFF", "ON"])


if __name__ == "__main__":
    unittest.main()
