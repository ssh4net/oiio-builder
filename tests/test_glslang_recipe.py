from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.recipes import glslang


ALIAS_RULES = r'''
# Unrelated user text.
set(legacy_glslang_name "glslangValidator${CMAKE_EXECUTABLE_SUFFIX}")
set(link_method create_symlink)
if(WIN32 OR MINGW)
  set(link_method copy_if_different)
endif()
add_custom_command(
    TARGET glslang-standalone POST_BUILD
    COMMAND "${CMAKE_COMMAND}" -E "${link_method}" "\$<TARGET_FILE_NAME:glslang-standalone>" "${legacy_glslang_name}"
    WORKING_DIRECTORY "\$<TARGET_FILE_DIR:glslang-standalone>"
    VERBATIM
)
if(GLSLANG_ENABLE_INSTALL)
    install(TARGETS glslang-standalone EXPORT glslang-targets)
    install(CODE "\
        message(STATUS \"Installing (${link_method}): \$<TARGET_FILE_NAME:glslang-standalone> -> \$ENV{DESTDIR}\${CMAKE_INSTALL_PREFIX}/${CMAKE_INSTALL_BINDIR}\")
        execute_process(
            COMMAND \"\${CMAKE_COMMAND}\" -E ${link_method} [=[\$<TARGET_FILE_NAME:glslang-standalone>]=] [=[${legacy_glslang_name}]=]
            WORKING_DIRECTORY \"\$ENV{DESTDIR}\${CMAKE_INSTALL_PREFIX}/${CMAKE_INSTALL_BINDIR}\"
        )
    ")
endif()
'''


def _fixture(root: Path, newline: str = "\n") -> Path:
    standalone = root / "StandAlone"
    standalone.mkdir(parents=True)
    path = standalone / "CMakeLists.txt"
    text = ('add_executable(glslang-standalone main.c)\n'
            'set_target_properties(glslang-standalone PROPERTIES OUTPUT_NAME glslang)\n' + ALIAS_RULES)
    path.write_bytes(text.replace("\n", newline).encode())
    return path


def _builder(root: Path, *, opt: bool = False, dry_run: bool = False):
    return SimpleNamespace(
        dry_run=dry_run, config=SimpleNamespace(global_cfg=SimpleNamespace(build_root=root / "build")),
        _repo_cmake_effective_toml_options=lambda _: SimpleNamespace(cache={"ENABLE_OPT": opt}),
    )


class GlslangRecipeTests(unittest.TestCase):
    def test_alias_patch_runs_without_opt_and_preserves_line_endings_and_user_text(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = _fixture(root, newline)
                builder = _builder(root)
                glslang.patch_source(builder, root)
                first = path.read_bytes()
                glslang.patch_source(builder, root)
                self.assertEqual(first, path.read_bytes())
                self.assertNotIn(b"create_symlink", first)
                self.assertIn(b"# Unrelated user text.", first)
                self.assertIn(b"${CMAKE_EXECUTABLE_SUFFIX}", first)
                self.assertIn(b"TARGET_FILE_DIR:glslang-standalone", first)
                self.assertIn(b"COMMAND_ERROR_IS_FATAL ANY", first)
                if newline == "\r\n":
                    self.assertNotIn(b"\n", first.replace(b"\r\n", b""))

    def test_dry_run_and_missing_alias_sources_are_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _fixture(root)
            before = path.read_bytes()
            glslang.patch_source(_builder(root, dry_run=True), root)
            self.assertEqual(before, path.read_bytes())
            glslang.patch_source(_builder(root), root / "missing")

    def test_source_staging_stamp_hit_does_not_skip_alias_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = _fixture(root)
            builder = _builder(root, opt=True)
            builder.repo_paths = {"SPIRV-Tools": root, "SPIRV-Headers": root}
            stamp = {"enable_opt": "1", "glslang_head": "head", "spirv_tools_head": "head",
                     "spirv_headers_head": "head"}
            with patch.object(glslang, "git_head", return_value="head"), \
                    patch.object(glslang, "_read_source_stamp", return_value=stamp), \
                    patch.object(glslang, "_copy_repo_tree") as copy:
                glslang.patch_source(builder, root)
            copy.assert_not_called()
            self.assertNotIn("create_symlink", path.read_text())

    @unittest.skipUnless(os.name != "nt" and shutil.which("cmake") and shutil.which("cc") and shutil.which("ninja"),
                         "POSIX CMake, Ninja and C compiler are needed for executable alias checks")
    def test_build_rebuild_and_staged_install_replace_links_with_executable_copies(self):
        for generator, mode in (("Ninja", "Debug"), ("Ninja Multi-Config", "Release")):
            with self.subTest(generator=generator), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source with spaces"
                _fixture(source)
                (source / "CMakeLists.txt").write_text(
                    "cmake_minimum_required(VERSION 3.22.1)\nproject(glslang_alias_fixture C)\n"
                    "include(GNUInstallDirs)\nset(GLSLANG_ENABLE_INSTALL ON)\nadd_subdirectory(StandAlone)\n"
                )
                main = source / "StandAlone/main.c"
                main.write_text('#include <stdio.h>\nint main(void) { puts("first"); return 0; }\n')
                glslang.patch_source(_builder(root), source)
                build = root / "build with spaces"
                target_dir = build / "StandAlone" / (mode if generator == "Ninja Multi-Config" else "")
                target_dir.mkdir(parents=True)
                alias = target_dir / "glslangValidator"
                alias.symlink_to("glslang")
                def run(command, env=None):
                    result = subprocess.run(command, capture_output=True, text=True, env=env)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    return result.stdout.strip()
                run(["cmake", "-S", str(source), "-B", str(build), "-G", generator,
                     f"-DCMAKE_BUILD_TYPE={mode}", f"-DCMAKE_INSTALL_PREFIX={root / 'unused-prefix'}"])
                for text in ("first", "second"):
                    main.write_text(f'#include <stdio.h>\nint main(void) {{ puts("{text}"); return 0; }}\n')
                    run(["cmake", "--build", str(build), "--config", mode, "--parallel", "2"])
                    self.assertFalse(alias.is_symlink())
                    self.assertTrue(os.access(alias, os.X_OK))
                    self.assertEqual((target_dir / "glslang").read_bytes(), alias.read_bytes())
                    self.assertEqual(text, run([str(alias)]))
                    if text == "first":
                        alias.unlink()
                        alias.symlink_to("glslang")
                prefix = root / "relocated prefix"
                staging = root / "staging with spaces"
                installed = staging / prefix.relative_to(prefix.anchor) / "bin"
                installed.mkdir(parents=True)
                (installed / "glslangValidator").symlink_to("glslang")
                run(["cmake", "--install", str(build), "--config", mode, "--prefix", str(prefix)],
                    dict(os.environ, DESTDIR=str(staging)))
                legacy = installed / "glslangValidator"
                self.assertFalse(legacy.is_symlink())
                self.assertTrue(os.access(legacy, os.X_OK))
                self.assertEqual((installed / "glslang").read_bytes(), legacy.read_bytes())
                self.assertEqual("second", run([str(legacy)]))
                self.assertFalse((root / "unused-prefix").exists())


if __name__ == "__main__":
    unittest.main()
