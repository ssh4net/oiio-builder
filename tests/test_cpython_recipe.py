from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.recipes import cpython


SHAREDMODS = '''LN = ln
MKDIR_P = mkdir -p
SHAREDMODS = Modules/array.so Modules/math.so
.PHONY: sharedmods
sharedmods: $(SHAREDMODS) pybuilddir.txt
\t@target=\x60cat pybuilddir.txt\x60; \\
\t$(MKDIR_P) $$target; \\
\tfor mod in X $(SHAREDMODS); do \\
\t\tif test $$mod != X; then \\
\t\t\t$(LN) -sf ../../$$mod $$target/\x60basename $$mod\x60; \\
\t\tfi; \\
\tdone
other-link:
\t$(LN) -f libpython.so.1 libpython.so
'''


class CPythonRecipeTests(unittest.TestCase):
    @unittest.skipUnless(os.name != "nt" and shutil.which("make"), "POSIX make is required")
    def test_extension_staging_works_without_symlinks_and_refreshes_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build = root / "build"
            (build / "Modules").mkdir(parents=True)
            for name in ("array.so", "math.so"):
                (build / "Modules" / name).write_bytes(b"extension fixture " + name.encode())
            (build / "pybuilddir.txt").write_text("build/lib.fixture\n", encoding="utf-8")
            makefile = build / "Makefile"
            makefile.write_text(SHAREDMODS, encoding="utf-8")
            tools = root / "tools"
            tools.mkdir()
            link = tools / "ln"
            link.write_text("#!/bin/sh\necho 'symlinks unavailable' >&2\nexit 1\n", encoding="utf-8")
            link.chmod(0o755)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
            command = [shutil.which("make"), "sharedmods"]
            before = subprocess.run(command, cwd=build, env=env, capture_output=True, text=True)
            self.assertNotEqual(0, before.returncode)
            self.assertIn("symlinks unavailable", before.stderr)
            # Existing POSIX builds may already contain a link to the source
            # module. It must be replaced rather than followed by cp.
            legacy = build / "build/lib.fixture/array.so"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.symlink_to("../../Modules/array.so")

            builder = SimpleNamespace(platform=SimpleNamespace(os="linux"), dry_run=False)
            cpython._copy_posix_shared_modules(builder, build)
            patched = makefile.read_bytes()
            cpython._copy_posix_shared_modules(builder, build)
            self.assertEqual(patched, makefile.read_bytes())
            self.assertIn(b"LN = ln", patched)
            self.assertIn(b"$(LN) -f libpython.so.1 libpython.so", patched)
            self.assertIn(b'test -n "$$target"', patched)
            for content in (b"first version", b"updated extension"):
                (build / "Modules/array.so").write_bytes(content)
                run = subprocess.run(command, cwd=build, env=env, capture_output=True, text=True)
                self.assertEqual(0, run.returncode, run.stdout + run.stderr)
                for name in ("array.so", "math.so"):
                    copied = build / "build/lib.fixture" / name
                    self.assertFalse(copied.is_symlink())
                    self.assertEqual((build / "Modules" / name).read_bytes(), copied.read_bytes())

    def test_staging_patch_skips_windows_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            makefile = root / "Makefile"
            makefile.write_text(SHAREDMODS, encoding="utf-8")
            original = makefile.read_bytes()
            for os_name, dry_run in (("windows", False), ("linux", True), ("macos", True)):
                builder = SimpleNamespace(platform=SimpleNamespace(os=os_name), dry_run=dry_run)
                cpython._copy_posix_shared_modules(builder, root)
                self.assertEqual(original, makefile.read_bytes())

    def test_posix_recipe_patches_makefile_after_configure_before_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "configure").touch()
            build = root / "build"
            ctx = SimpleNamespace(src_dir=source, build_dir=build, install_prefix=root / "prefix",
                                  repo=SimpleNamespace(name="cpython"), build_type="Debug")
            builder = SimpleNamespace(platform=SimpleNamespace(os="linux"), dry_run=False, toolchain={},
                                      _normalize_posix_shell_scripts=lambda *args: None,
                                      _non_cmake_flags=lambda *args: ("", "", ""),
                                      _repo_log_path=lambda *args: root / "log", _jobs=lambda: 2)

            def fake_run(command, **kwargs):
                if command[0] == str(source / "configure"):
                    (build / "Makefile").write_text(SHAREDMODS, encoding="utf-8")
                else:
                    generated = (build / "Makefile").read_text()
                    self.assertNotIn("$(LN) -sf ../../$$mod", generated)
                    self.assertIn('cp -f "$$mod"', generated)

            with patch.object(cpython, "run", side_effect=fake_run) as run:
                cpython._build_posix(builder, ctx, {})
                self.assertEqual(3, run.call_count)
                self.assertEqual(["make", "-j2"], run.call_args_list[1].args[0])
                self.assertEqual(["make", "install"], run.call_args_list[2].args[0])


if __name__ == "__main__":
    unittest.main()
