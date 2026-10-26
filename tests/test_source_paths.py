from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.config import RepoConfig
from builder.core import Builder
from builder.platform import PlatformInfo
from builder.source_paths import discover_source_paths, source_registry_files


def _builder(root: Path) -> Builder:
    builder = object.__new__(Builder)
    builder.config = SimpleNamespace(
        global_cfg=SimpleNamespace(src_root=root / "sources"),
        repos=[RepoConfig(name="imgui", dir="imgui"),
               RepoConfig(name="imgui_test_engine", dir="imgui_test_engine"),
               RepoConfig(name="toml11", dir="toml11")],
        build_types=["Debug", "Release"],
    )
    builder.platform = PlatformInfo(os="linux", arch="x86_64")
    builder.prefixes = {"Debug": root / "prefix", "Release": root / "prefix"}
    builder.repo_paths = {}
    builder.dry_run = False
    return builder


def _write_registry(prefix: Path, sources: dict[str, str]) -> None:
    for relative, content in source_registry_files(sources).items():
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SourcePathTests(unittest.TestCase):
    def test_cli_registration_is_metadata_only_and_rejects_build_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            checkout = root / "sources" / "imgui"
            checkout.mkdir(parents=True)
            header = checkout / "imgui.h"
            header.write_text("preserve source", encoding="utf-8")
            config = root / "build.toml"
            config.write_text(
                "[global]\n"
                f"src_root = {json.dumps((root / 'sources').as_posix())}\n"
                f"build_root = {json.dumps((root / 'build').as_posix())}\n"
                f"install_prefix = {json.dumps((root / 'prefix').as_posix())}\n"
                'prefix_layout = "suffix"\n'
                'use_libcxx = false\nuse_lld = false\nuse_ccache = false\n'
                '[[repos]]\nname = "imgui"\ndir = "imgui"\nbuild_system = "must-not-run"\n',
                encoding="utf-8",
            )
            command = [sys.executable, str(Path(__file__).parents[1] / "build.py"),
                       "--config", str(config), "--no-user-config", "--build-types", "Release",
                       "--register-sources"]
            dry = subprocess.run([*command, "--dry-run"], capture_output=True, text=True)
            self.assertEqual(0, dry.returncode, dry.stdout + dry.stderr)
            self.assertFalse((root / "prefix").exists())
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            manifests = list(root.rglob("source-paths.json"))
            self.assertEqual(1, len(manifests))
            self.assertEqual(checkout.as_posix(), json.loads(manifests[0].read_text())["sources"]["imgui"])
            self.assertFalse(list(root.rglob("prefix-contract.json")))
            self.assertFalse((root / "build").exists())
            self.assertEqual("preserve source", header.read_text())
            for operation in ("--prepare-only", "--update-only", "--force"):
                rejected = subprocess.run([*command, operation], capture_output=True, text=True)
                self.assertEqual(2, rejected.returncode)
                self.assertIn("--register-sources cannot be combined", rejected.stderr)

    def test_metadata_only_registration_keeps_all_existing_configured_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root)
            sources = builder.config.global_cfg.src_root
            for name in ("imgui", "toml11"):
                (sources / name).mkdir(parents=True)
            # A narrow build has only this repo in repo_paths; other registrations
            # must still come from the full configured graph.
            builder.repo_paths = {"toml11": sources / "toml11"}
            prefix = root / "prefix"
            (prefix / "lib").mkdir(parents=True)
            library = prefix / "lib" / "existing.lib"
            library.write_bytes(b"preserve")
            with patch("builder.core.ensure_repo", side_effect=AssertionError("must not sync")):
                self.assertEqual(0, builder.register_sources())
            manifest = prefix / ".oiio-builder" / "source-paths.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual({"imgui", "toml11"}, set(payload["sources"]))
            self.assertEqual((sources / "imgui").absolute().as_posix(), payload["sources"]["imgui"])
            self.assertEqual(b"preserve", library.read_bytes())
            self.assertFalse((prefix / ".oiio-builder" / "prefix-contract.json").exists())
            before = manifest.stat().st_mtime_ns
            builder.register_sources()
            self.assertEqual(before, manifest.stat().st_mtime_ns)
            # A removed checkout must not remain registered on the next refresh.
            (sources / "imgui").rmdir()
            builder.register_sources()
            self.assertEqual({"toml11"}, set(json.loads(manifest.read_text())["sources"]))

    def test_existing_candidates_and_resolved_paths_use_repo_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root)
            alternate = root / "sources" / "imgui-docking"
            alternate.mkdir(parents=True)
            builder.config.repos[0].dir_candidates = ["imgui-docking"]
            selected = root / "selected engine"
            selected.mkdir()
            builder.repo_paths["imgui_test_engine"] = selected
            builder.config.repos[1].source_subdir = "subproject"
            sources = discover_source_paths(builder.config, builder.repo_paths)
            self.assertEqual(alternate.as_posix(), sources["imgui"])
            self.assertEqual(selected.as_posix(), sources["imgui_test_engine"])

    def test_source_metadata_alone_does_not_block_prefix_contract_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root)
            builder.register_sources()
            prefix = root / "prefix"
            self.assertFalse(builder._prefix_has_non_metadata_content(prefix))
            self.assertEqual("missing-empty", builder.check_prefix_contract(prefix, ["Release"]).state)
            with patch("builder.source_paths.Path.rglob", side_effect=PermissionError):
                self.assertTrue(builder._prefix_has_non_metadata_content(prefix))
            other = prefix / "share" / "cmake" / "another-package"
            other.mkdir()
            self.assertTrue(builder._prefix_has_non_metadata_content(prefix))
            self.assertEqual("missing-populated", builder.check_prefix_contract(prefix, ["Release"]).state)

    def test_dry_run_does_not_create_prefix_or_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root)
            builder.dry_run = True
            builder.repo_paths["imgui"] = root / "missing checkout"
            builder.register_sources()
            self.assertFalse((root / "prefix").exists())
            self.assertFalse((root / "missing checkout").exists())

    @unittest.skipUnless(shutil.which("cmake"), "CMake is needed for package discovery tests")
    def test_real_cmake_discovers_paths_without_targets_and_reports_stale_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            imgui = root / "source ${literal} ; [=]"
            engine = root / "engine"
            imgui.mkdir()
            engine.mkdir()
            raw_source = root / "raw source"
            raw_cmake = root / "raw cmake"
            raw_source.mkdir()
            raw_cmake.mkdir()
            sources = {"imgui": imgui.as_posix(), "imgui_test_engine": engine.as_posix(),
                       "LibRaw": raw_source.as_posix(), "libraw": raw_cmake.as_posix()}
            _write_registry(prefix, sources)
            script = root / "consumer.cmake"
            result = root / "result.txt"
            script.write_text('''
set(OIIOBuilderSources_absent_FOUND TRUE)
find_package(OIIOBuilderSources CONFIG REQUIRED COMPONENTS imgui imgui_test_engine LibRaw libraw
             OPTIONAL_COMPONENTS absent)
if(OIIOBuilderSources_absent_FOUND)
  message(FATAL_ERROR "Missing optional checkout was reported as found")
endif()
oiio_builder_source_dir(imgui IMGUI_DIR)
oiio_builder_source_dir(imgui_test_engine IMGUI_TEST_ENGINE_DIR)
oiio_builder_source_dir(LibRaw LIBRAW_SOURCE)
oiio_builder_source_dir(libraw LIBRAW_CMAKE)
if(LIBRAW_SOURCE STREQUAL LIBRAW_CMAKE)
  message(FATAL_ERROR "Case-sensitive repository names collided")
endif()
if(TARGET imgui OR TARGET imgui::imgui OR DEFINED BUILD_SHARED_LIBS)
  message(FATAL_ERROR "Source discovery changed build configuration")
endif()
file(WRITE "${OUTPUT_FILE}" "${IMGUI_DIR}\\n${IMGUI_TEST_ENGINE_DIR}")
''', encoding="utf-8")
            command = ["cmake", f"-DCMAKE_PREFIX_PATH={prefix.as_posix()}", f"-DOUTPUT_FILE={result.as_posix()}",
                       "-P", str(script)]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)
            self.assertEqual(imgui.as_posix() + "\n" + engine.as_posix(), result.read_text())
            # The directory was present at registration but disappeared later.
            engine.rmdir()
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, run.returncode)
            self.assertIn("imgui_test_engine", run.stderr)
            self.assertIn("no longer exists", run.stderr)

    @unittest.skipUnless(shutil.which("cmake"), "CMake is needed for package discovery tests")
    def test_switching_prefixes_clears_previous_source_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            _write_registry(root / "prefix1", {"imgui": first.as_posix()})
            _write_registry(root / "prefix2", {"toml11": second.as_posix()})
            script = root / "switch.cmake"
            script.write_text('''
find_package(OIIOBuilderSources CONFIG REQUIRED COMPONENTS imgui)
set(OIIOBuilderSources_DIR "${SECOND_PREFIX}/share/cmake/OIIOBuilderSources")
find_package(OIIOBuilderSources CONFIG REQUIRED COMPONENTS toml11)
if(DEFINED OIIOBuilderSources_SOURCE_DIR_imgui)
  message(FATAL_ERROR "Source map retained a repository from the previous prefix")
endif()
oiio_builder_source_dir(toml11 TOML_SOURCE_DIR)
''', encoding="utf-8")
            run = subprocess.run(["cmake", f"-DCMAKE_PREFIX_PATH={(root / 'prefix1').as_posix()}",
                                  f"-DSECOND_PREFIX={(root / 'prefix2').as_posix()}", "-P", str(script)],
                                 capture_output=True, text=True)
            self.assertEqual(0, run.returncode, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
