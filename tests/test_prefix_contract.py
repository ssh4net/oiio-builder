from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.core import Builder
from builder.platform import PlatformInfo


def _builder(prefix: Path, *, static: bool) -> Builder:
    builder = object.__new__(Builder)
    builder.config = SimpleNamespace(
        global_cfg=SimpleNamespace(
            write_prefix_contract=True,
            use_libcxx=False,
            env={},
            static_default=static,
            pic=True,
            cxx_standard=20,
            cxx_extensions=False,
            use_lld=True,
            windows={},
        ),
        build_types=["Release"],
    )
    builder.platform = PlatformInfo(os="linux", arch="x86_64")
    builder.license_profile = None
    builder._license_profile_exclusions = {}
    builder.apply_prefix_contract = False
    builder.prefixes = {"Release": prefix}
    builder.toolchain = {}
    builder.dry_run = False
    builder.repos = []
    return builder


class PrefixContractTests(unittest.TestCase):
    def test_populated_prefix_rejects_linkage_change_without_overwriting_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            builder = _builder(prefix, static=True)
            builder._ensure_prefix_contracts()
            contract_path = prefix / ".oiio-builder" / "prefix-contract.json"
            original_contract = contract_path.read_text(encoding="utf-8")
            lib_dir = prefix / "lib"
            lib_dir.mkdir()
            (lib_dir / "libexample.a").touch()

            builder.config.global_cfg.static_default = False
            with self.assertRaisesRegex(
                RuntimeError,
                "Refusing to overwrite an incompatible contract",
            ):
                builder._ensure_prefix_contracts()

            self.assertEqual(contract_path.read_text(encoding="utf-8"), original_contract)

    def test_populated_prefix_without_contract_is_not_adopted_implicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "prefix"
            lib_dir = prefix / "lib"
            lib_dir.mkdir(parents=True)
            (lib_dir / "libexample.a").touch()
            builder = _builder(prefix, static=True)

            with self.assertRaisesRegex(
                RuntimeError,
                "Refusing to adopt populated prefix",
            ):
                builder._ensure_prefix_contracts()

            self.assertFalse((prefix / ".oiio-builder").exists())


if __name__ == "__main__":
    unittest.main()
