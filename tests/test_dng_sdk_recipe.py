from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import dng_sdk


def _write_sdk_archive(path: Path, top: str, version_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{top}/dng_sdk/source/dng_host.h", f"// {version_text}\n")
        archive.writestr(f"{top}/xmp/toolkit/public/include/XMP.hpp", "// xmp\n")
        archive.writestr(f"{top}/xmp/toolkit/XMPCore/source/WXMPDocOps.cpp", "// xmp core\n")


def _builder(root: Path, env: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(global_cfg=SimpleNamespace(repo_root=root, build_root=root / "_build")),
        _source_prep_env=lambda: dict(env or {}),
        dry_run=False,
    )


class DngSdkArchiveSelectionTests(unittest.TestCase):
    def test_candidates_are_ordered_by_numeric_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            external = Path(tmp)
            for name in (
                "dng_sdk_1_7_1.zip",
                "dng_sdk_1_7_1_0.zip",
                "dng_sdk_1_7_1_2724_20260908.zip",
                "dng_sdk_1_6_0.tar.gz",
                "dng_sdk_1_10_0.tgz",
                "My_DNG_SDK.zip",
                "vcpkg-export-libvpx.zip",
                "dng_sdk_notes.txt",
            ):
                (external / name).write_bytes(b"")
            (external / "dng_sdk_1_8_0.zip").mkdir()

            names = [path.name for path in dng_sdk.archive_candidates(external)]

        self.assertEqual(
            names,
            [
                "dng_sdk_1_10_0.tgz",
                "dng_sdk_1_7_1_2724_20260908.zip",
                "dng_sdk_1_7_1_0.zip",
                "dng_sdk_1_7_1.zip",
                "dng_sdk_1_6_0.tar.gz",
                "My_DNG_SDK.zip",
            ],
        )

    def test_resolve_prefers_newest_archive_then_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "external").mkdir()
            (root / "external" / "dng_sdk_1_7_1.zip").write_bytes(b"")
            (root / "external" / "dng_sdk_1_7_1_2724_20260908.zip").write_bytes(b"")

            self.assertEqual(
                dng_sdk.resolve_archive({}, root).name,
                "dng_sdk_1_7_1_2724_20260908.zip",
            )
            self.assertEqual(
                dng_sdk.resolve_archive({"DNGSDK_ARCHIVE": '"external/dng_sdk_1_7_1.zip"'}, root),
                (root / "external" / "dng_sdk_1_7_1.zip").resolve(),
            )
            self.assertEqual(
                dng_sdk.resolve_archive({"DNG_SDK_ARCHIVE": str(root / "sdk")}, root),
                root / "sdk",
            )

    def test_resolve_returns_none_without_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(dng_sdk.resolve_archive({}, Path(tmp)))


class DngSdkStagingTests(unittest.TestCase):
    def test_stages_sources_and_records_the_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "DNG-CMake"
            src.mkdir()
            _write_sdk_archive(root / "external" / "dng_sdk_1_7_1_2724_20260908.zip", "dng_sdk_1_7_1", "new")

            dng_sdk._stage_sdk_sources(_builder(root), src)

            self.assertEqual((src / "dng_sdk" / "source" / "dng_host.h").read_text(), "// new\n")
            self.assertTrue((src / "xmp" / "toolkit" / "XMPCore" / "source" / "WXMPDocOps.cpp").exists())
            marker = (src / "dng_sdk" / ".oiio-builder-source").read_text().splitlines()
            self.assertTrue(marker[0].startswith("sha256:"))
            self.assertTrue(marker[1].endswith("dng_sdk_1_7_1_2724_20260908.zip"))

    def test_restages_legacy_sources_without_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "DNG-CMake"
            _write_sdk_archive(root / "old.zip", "dng_sdk_1_7_1", "old")
            with zipfile.ZipFile(root / "old.zip") as archive:
                archive.extractall(root / "old")
            (src / "dng_sdk").parent.mkdir(parents=True)
            (root / "old" / "dng_sdk_1_7_1" / "dng_sdk").rename(src / "dng_sdk")
            (root / "old" / "dng_sdk_1_7_1" / "xmp").rename(src / "xmp")
            (src / "dng_sdk" / "source" / "stale_only_in_old.cpp").write_text("// stale\n")
            _write_sdk_archive(root / "external" / "dng_sdk_1_7_1_2724_20260908.zip", "dng_sdk_1_7_1", "new")

            dng_sdk._stage_sdk_sources(_builder(root), src)

            self.assertEqual((src / "dng_sdk" / "source" / "dng_host.h").read_text(), "// new\n")
            self.assertFalse((src / "dng_sdk" / "source" / "stale_only_in_old.cpp").exists())

    def test_keeps_sources_staged_from_the_same_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "DNG-CMake"
            src.mkdir()
            _write_sdk_archive(root / "external" / "dng_sdk_1_7_1_2724_20260908.zip", "dng_sdk_1_7_1", "new")
            builder = _builder(root)
            dng_sdk._stage_sdk_sources(builder, src)
            sentinel = src / "dng_sdk" / "source" / "sentinel.txt"
            sentinel.write_text("kept\n")

            dng_sdk._stage_sdk_sources(builder, src)

            self.assertTrue(sentinel.exists())

    def test_newer_archive_replaces_staged_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "DNG-CMake"
            src.mkdir()
            _write_sdk_archive(root / "external" / "dng_sdk_1_7_1.zip", "dng_sdk_1_7_1", "old")
            builder = _builder(root)
            dng_sdk._stage_sdk_sources(builder, src)
            payload_old: dict = {}
            dng_sdk.stamp_payload(builder, None, SimpleNamespace(src_dir=src), payload_old)

            _write_sdk_archive(root / "external" / "dng_sdk_1_7_1_2724_20260908.zip", "dng_sdk_1_7_1", "new")
            payload_new: dict = {}
            dng_sdk.stamp_payload(builder, None, SimpleNamespace(src_dir=src), payload_new)
            dng_sdk._stage_sdk_sources(builder, src)

            self.assertNotEqual(payload_old["dng_sdk_source"], payload_new["dng_sdk_source"])
            self.assertEqual((src / "dng_sdk" / "source" / "dng_host.h").read_text(), "// new\n")
            marker = (src / "dng_sdk" / ".oiio-builder-source").read_text().splitlines()[0]
            self.assertEqual(marker, payload_new["dng_sdk_source"])

    def test_renamed_archive_keeps_the_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "external" / "dng_sdk_1_7_1_2724_20260908.zip"
            _write_sdk_archive(archive, "dng_sdk_1_7_1", "new")
            builder = _builder(root)
            ctx = SimpleNamespace(src_dir=root / "DNG-CMake")
            before: dict = {}
            dng_sdk.stamp_payload(builder, None, ctx, before)

            archive.rename(root / "external" / "dng_sdk_1_7_1_2724.zip")
            after: dict = {}
            dng_sdk.stamp_payload(builder, None, ctx, after)

            self.assertEqual(before, after)

    def test_previously_staged_sources_are_used_without_an_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "DNG-CMake"
            src.mkdir()
            archive = root / "external" / "dng_sdk_1_7_1_2724_20260908.zip"
            _write_sdk_archive(archive, "dng_sdk_1_7_1", "new")
            builder = _builder(root)
            dng_sdk._stage_sdk_sources(builder, src)
            staged: dict = {}
            dng_sdk.stamp_payload(builder, None, SimpleNamespace(src_dir=src), staged)
            archive.unlink()

            dng_sdk._stage_sdk_sources(builder, src)
            without_archive: dict = {}
            dng_sdk.stamp_payload(builder, None, SimpleNamespace(src_dir=src), without_archive)

            self.assertEqual((src / "dng_sdk" / "source" / "dng_host.h").read_text(), "// new\n")
            self.assertEqual(staged, without_archive)

    def test_missing_archive_and_sources_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "DNG-CMake").mkdir()
            with self.assertRaisesRegex(RuntimeError, "missing Adobe DNG SDK source archive"):
                dng_sdk._stage_sdk_sources(_builder(root), root / "DNG-CMake")

    def test_missing_explicit_archive_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "DNG-CMake").mkdir()
            builder = _builder(root, {"DNGSDK_ARCHIVE": str(root / "absent.zip")})
            with self.assertRaisesRegex(RuntimeError, "archive path does not exist"):
                dng_sdk._stage_sdk_sources(builder, root / "DNG-CMake")


if __name__ == "__main__":
    unittest.main()
