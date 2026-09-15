from __future__ import annotations

import json
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
    def test_legacy_contract_upgrade_preserves_compatibility_checks(self):
        for change in ("none", "linkage", "definitions", "unknown-schema", "missing-definitions"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                prefix = Path(tmp) / "prefix"
                builder = _builder(prefix, static=True)
                builder._ensure_prefix_contracts()
                contract_path = prefix / ".oiio-builder" / "prefix-contract.json"
                payload = json.loads(contract_path.read_text(encoding="utf-8"))
                payload["schema"] = 1
                del payload["license"]
                if change == "unknown-schema":
                    payload["schema"] = 99
                elif change == "missing-definitions":
                    payload["schema"] = 2
                original = json.dumps(payload)
                contract_path.write_text(original, encoding="utf-8")
                lib_dir = prefix / "lib"
                lib_dir.mkdir()
                library = lib_dir / "libexample.a"
                library.write_bytes(b"existing library")
                if change == "linkage":
                    builder.config.global_cfg.static_default = False
                elif change == "definitions":
                    builder.license_profile = SimpleNamespace(
                        name=None, linkage=None,
                        consumer_compile_definitions=("EIGEN_MPL2_ONLY",),
                    )

                if change == "none":
                    self.assertEqual(builder.check_prefix_contract(prefix, ["Release"]).state, "ok")
                    self.assertEqual(contract_path.read_text(encoding="utf-8"), original)
                    builder._ensure_prefix_contracts()
                    upgraded = json.loads(contract_path.read_text(encoding="utf-8"))
                    self.assertEqual(upgraded["schema"], 2)
                    self.assertEqual(upgraded["license"]["consumer_compile_definitions"], [])
                else:
                    with self.assertRaisesRegex(RuntimeError, "incompatible contract"):
                        builder._ensure_prefix_contracts()
                    self.assertEqual(contract_path.read_text(encoding="utf-8"), original)
                self.assertEqual(library.read_bytes(), b"existing library")

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
