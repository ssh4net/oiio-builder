from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from builder.config import load_config
from builder.core import Builder
from builder.license_policy import rejected_reason, resolve_profile
from builder.platform import PlatformInfo
from builder.recipes import libfuzzer, registry

ROOT = Path(__file__).parents[1]


class LibFuzzerRecipeTests(unittest.TestCase):
    def test_opt_in_and_license_profiles(self):
        config = load_config(ROOT / "build.toml", load_user_config=False)
        repo = next(r for r in config.repos if r.name == "libfuzzer")
        self.assertFalse(config.global_cfg.build_libfuzzer)
        self.assertTrue(repo.optional)
        for os_name in ("linux", "macos", "windows"):
            builder = object.__new__(Builder)
            builder.config = config
            builder.platform = PlatformInfo(os=os_name, arch="x86_64")
            builder.license_profile = None
            self.assertFalse(registry.enabled(repo.name, builder, repo))
            config.global_cfg.build_libfuzzer = True
            config.only = {repo.name}
            self.assertEqual([r.name for r in builder._filter_repos()], [repo.name])
            config.global_cfg.build_libfuzzer = False
        for profile in ("nongpl-static", "lgpl-dynamic"):
            self.assertIsNone(rejected_reason(resolve_profile(profile), repo.name))
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "build.user.toml"
            override.write_text("[global]\nbuild_libfuzzer = true\n")
            self.assertTrue(load_config(ROOT / "build.toml", user_config_path=override)
                            .global_cfg.build_libfuzzer)

    def _repo(self):
        return SimpleNamespace(name="libfuzzer", url=libfuzzer.URL, ref=libfuzzer.VERSION,
                               source_subdir=".oiio-builder-cmake")

    def _archive(self, path, extra=None):
        with tarfile.open(path, "w:xz") as archive:
            for relative in ("lib/fuzzer/FuzzerMain.cpp", "include/fuzzer/FuzzedDataProvider.h", "LICENSE.TXT"):
                member = tarfile.TarInfo(f"{libfuzzer.ARCHIVE_ROOT}/{relative}")
                member.size = 5
                archive.addfile(member, io.BytesIO(b"test\n"))
            if extra:
                archive.addfile(extra)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_archive_acquisition_reuse_and_unknown_directory_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "source.tar.xz"
            checksum = self._archive(archive)
            builder = SimpleNamespace(dry_run=False,
                _source_prep_env=Mock(return_value={"LIBFUZZER_SOURCE_ARCHIVE": str(archive)}))
            target = root / "source"
            with patch.object(libfuzzer, "SHA256", checksum), patch.object(libfuzzer.urllib.request, "urlopen") as urlopen:
                self.assertTrue(registry.sync_source("libfuzzer", builder, self._repo(), target))
                self.assertTrue((target / libfuzzer.MARKER).is_file())
                self.assertTrue(registry.sync_source("libfuzzer", builder, self._repo(), target))
                urlopen.assert_not_called()
                (target / libfuzzer.MARKER).unlink()
                with self.assertRaisesRegex(RuntimeError, "preserving unmanaged"):
                    libfuzzer.sync_source(builder, self._repo(), target)
                self.assertEqual((target / "LICENSE.TXT").read_text(), "test\n")

    def test_checksum_failure_leaves_destination_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "source.tar.xz"
            self._archive(archive)
            builder = SimpleNamespace(dry_run=False,
                _source_prep_env=lambda: {"LIBFUZZER_SOURCE_ARCHIVE": str(archive)})
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                libfuzzer.sync_source(builder, self._repo(), root / "source")
            self.assertEqual(list(root.iterdir()), [archive])

    def test_oversized_download_is_bounded_and_cleans_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = SimpleNamespace(dry_run=False, _source_prep_env=lambda: {})
            with patch.object(libfuzzer, "MAX_ARCHIVE_BYTES", 4), \
                    patch.object(libfuzzer.urllib.request, "urlopen", return_value=io.BytesIO(b"oversized")):
                with self.assertRaisesRegex(RuntimeError, "size limit"):
                    libfuzzer.sync_source(builder, self._repo(), root / "source")
            self.assertEqual(list(root.iterdir()), [])

    def test_unsafe_members_rejected_before_extraction(self):
        for name, kind in (("../escape", tarfile.REGTYPE),
                           (f"{libfuzzer.ARCHIVE_ROOT}/../../escape", tarfile.REGTYPE),
                           (f"{libfuzzer.ARCHIVE_ROOT}/link", tarfile.SYMTYPE),
                           (f"{libfuzzer.ARCHIVE_ROOT}/C:escape", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                extra = tarfile.TarInfo(name)
                extra.type = kind
                extra.linkname = "../../escape"
                archive = root / "source.tar.xz"
                self._archive(archive, extra)
                with self.assertRaisesRegex(RuntimeError, "unsafe archive member"):
                    libfuzzer._extract_archive(archive, root / "out")
                self.assertFalse((root / "out").exists())

    def test_dry_run_and_rejected_override_have_no_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(libfuzzer.urllib.request, "urlopen") as download:
            root = Path(tmp)
            builder = SimpleNamespace(dry_run=True)
            repo = self._repo()
            libfuzzer.sync_source(builder, repo, root / "source")
            libfuzzer.patch_source(builder, root / "source")
            repo.ref = "unreviewed"
            with self.assertRaisesRegex(RuntimeError, "reviewed recipe"):
                libfuzzer.sync_source(builder, repo, root / "source")
            self.assertEqual(list(root.iterdir()), [])
            download.assert_not_called()

    def test_source_hook_bypasses_git_only_for_handled_recipes(self):
        builder = object.__new__(Builder)
        builder.dry_run = False
        builder.no_update = True
        builder.repo_paths = {}
        builder._resolve_repo_dir = Mock(return_value=Path("source"))
        builder._maybe_skip_missing = Mock(return_value=False)
        repo = self._repo()
        with patch.object(registry, "sync_source", return_value=True), patch("builder.core.ensure_repo") as git:
            builder._sync_repos([repo.name], {repo.name: repo})
            git.assert_not_called()
            builder._maybe_skip_missing.assert_not_called()
        self.assertFalse(registry.sync_source("zlib-ng", builder, repo, Path("source")))

    def test_wrapper_is_idempotent_and_source_changes_invalidate_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = SimpleNamespace(src_dir=root / ".oiio-builder-cmake")
            libfuzzer.patch_source(SimpleNamespace(dry_run=False), root)
            before = {p.name: p.stat().st_mtime_ns for p in ctx.src_dir.iterdir()}
            libfuzzer.patch_source(SimpleNamespace(dry_run=False), root)
            self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in ctx.src_dir.iterdir()})
            first = {}
            libfuzzer.stamp_payload(None, None, ctx, first)
            (root / "lib/fuzzer").mkdir(parents=True)
            (root / "lib/fuzzer/FuzzerMain.cpp").write_text("changed\n")
            second = {}
            libfuzzer.stamp_payload(None, None, ctx, second)
            self.assertNotEqual(first, second)
            (root / "include/sanitizer").mkdir(parents=True)
            (root / "include/sanitizer/lsan_interface.h").write_text("changed header\n")
            third = {}
            libfuzzer.stamp_payload(None, None, ctx, third)
            self.assertNotEqual(second, third)


if __name__ == "__main__":
    unittest.main()
