from __future__ import annotations

import stat
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder.recipes import openssl


def _builder(root: Path, *, os_name: str, static: bool, build_type: str = "Release", dry_run: bool = True):
    prefix = root / "prefix"
    windows = {"debug_postfix": "d", "msvc_runtime": "static" if static else "dynamic"}
    return SimpleNamespace(
        config=SimpleNamespace(global_cfg=SimpleNamespace(static_default=static, windows=windows)),
        platform=SimpleNamespace(os=os_name, arch="x86_64"),
        toolchain={"cc": "cl.exe" if os_name == "windows" else "cc", "ar": "ar", "ranlib": "ranlib"},
        prefixes={"Release": prefix, "Debug": prefix},
        dry_run=dry_run,
        _non_cmake_flags=lambda selected: (
            ("-O0 -g" if selected == "Debug" else "-O3 -DNDEBUG"),
            "",
            "",
        ),
    )


def _context(root: Path, build_type: str):
    return SimpleNamespace(
        repo=SimpleNamespace(name="openssl"),
        build_type=build_type,
        build_dir=root / "build",
        install_prefix=root / "prefix",
        src_dir=root / "src",
    )


class OpenSSLRecipeTests(unittest.TestCase):
    def test_openssl_is_not_tied_to_qt(self):
        self.assertTrue(openssl.enabled(None, None))

    def test_clean_build_dir_removes_readonly_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_dir = root / "build"
            generated = build_dir / "apps" / "CA.pl"
            generated.parent.mkdir(parents=True)
            generated.write_text("generated\n", encoding="utf-8")
            generated.chmod(stat.S_IREAD)
            builder = _builder(root, os_name="windows", static=False, dry_run=False)

            openssl._clean_build_dir(builder, build_dir)

            self.assertTrue(build_dir.is_dir())
            self.assertEqual(list(build_dir.iterdir()), [])

    def test_windows_debug_shared_uses_variant_and_isolated_install_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root, os_name="windows", static=False)
            ctx = _context(root, "Debug")
            command = openssl._configure_command(builder, ctx, {})

        self.assertIn("VC-WIN64A-oiio-debug", command)
        self.assertIn(f"--prefix={ctx.install_prefix / 'Debug'}", command)
        self.assertIn("shared", command)
        self.assertIn("--debug", command)
        self.assertTrue(any(arg.startswith("--config=") for arg in command))
        self.assertIn("no-tests", command)
        self.assertIn("no-makedepend", command)

    def test_windows_release_static_uses_standard_target_and_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root, os_name="windows", static=True)
            ctx = _context(root, "Release")
            command = openssl._configure_command(builder, ctx, {})

        self.assertIn("VC-WIN64A", command)
        self.assertNotIn("VC-WIN64A-oiio-debug", command)
        self.assertIn(f"--prefix={ctx.install_prefix}", command)
        self.assertIn("no-shared", command)
        self.assertIn("--release", command)
        self.assertFalse(any(arg.startswith("--config=") for arg in command))

    def test_wsl_linux_build_produces_linux_target_not_msvc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root, os_name="linux", static=False)
            ctx = _context(root, "Release")
            command = openssl._configure_command(builder, ctx, {})

        self.assertIn("linux-x86_64", command)
        self.assertIn("shared", command)
        self.assertNotIn("VC-WIN64A", command)

    @unittest.skipUnless(os.name != "nt" and (shutil.which("gmake") or shutil.which("make")),
                         "POSIX make is needed to verify helper copies")
    def test_posix_helpers_build_without_symlink_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _context(root, "Debug")
            builder = _builder(root, os_name="linux", static=True, dry_run=False)
            ctx.build_dir.mkdir()
            source_files = {"util/opensslwrap.sh": "#!/bin/sh\nexit 0\n", "apps/openssl.cnf": "# test config\n"}
            for name, content in source_files.items():
                source = ctx.src_dir / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content, encoding="utf-8")
                source.chmod(0o755 if name.endswith(".sh") else 0o644)
            makefile = ctx.build_dir / "Makefile"
            text = "SRCDIR = ../src\nBLDDIR = .\nlink-utils: $(BLDDIR)/util/opensslwrap.sh $(BLDDIR)/apps/openssl.cnf\n"
            for name in source_files:
                directory = name.split("/")[0]
                text += (
                    f"$(BLDDIR)/{name}: Makefile\n"
                    '\t@if [ "$(SRCDIR)" != "$(BLDDIR)" ]; then \\\n'
                    f'\t    mkdir -p "$(BLDDIR)/{directory}"; \\\n'
                    f'\t    ln -sf "../$(SRCDIR)/{directory}/`basename "$@"`" "$(BLDDIR)/{directory}"; \\\n'
                    '\tfi\n'
                )
            makefile.write_text(text, encoding="utf-8")
            tools = root / "tools"
            tools.mkdir()
            link = tools / "ln"
            link.write_text("#!/bin/sh\necho 'symlinks unavailable' >&2\nexit 1\n", encoding="utf-8")
            link.chmod(0o755)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}
            command = [shutil.which("gmake") or shutil.which("make"), "link-utils"]
            before = subprocess.run(command, cwd=ctx.build_dir, env=env, capture_output=True, text=True)
            self.assertNotEqual(0, before.returncode)
            self.assertIn("symlinks unavailable", before.stderr)

            openssl._patch_posix_helper_rules(builder, ctx)
            patched = makefile.read_bytes()
            openssl._patch_posix_helper_rules(builder, ctx)
            self.assertEqual(patched, makefile.read_bytes())
            after = subprocess.run(command, cwd=ctx.build_dir, env=env, capture_output=True, text=True)
            self.assertEqual(0, after.returncode, after.stdout + after.stderr)
            for name, content in source_files.items():
                destination = ctx.build_dir / name
                self.assertFalse(destination.is_symlink())
                self.assertEqual(content, destination.read_text())
                self.assertEqual(content, (ctx.src_dir / name).read_text())
            self.assertTrue(os.access(ctx.build_dir / "util/opensslwrap.sh", os.X_OK))

    def test_helper_rule_patch_skips_windows_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = _context(root, "Debug")
            # Neither skipped case needs a generated Makefile or creates files.
            for os_name, dry_run in (("windows", False), ("linux", True)):
                builder = _builder(root, os_name=os_name, static=True, dry_run=dry_run)
                openssl._patch_posix_helper_rules(builder, ctx)
            self.assertFalse(ctx.build_dir.exists())

    def test_debug_variant_file_sets_d_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(root, os_name="windows", static=False, dry_run=False)
            ctx = _context(root, "Debug")
            ctx.build_dir.mkdir()
            target, config_path = openssl._windows_variant_target(builder, ctx)
            text = config_path.read_text(encoding="utf-8") if config_path else ""

        self.assertEqual(target, "VC-WIN64A-oiio-debug")
        self.assertIn('inherit_from => [ "VC-WIN64A" ]', text)
        self.assertIn('shlib_variant => "d"', text)

    def test_windows_package_exports_multiconfig_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "VERSION.dat").write_text("MAJOR=4\nMINOR=0\nPATCH=0\n", encoding="utf-8")
            builder = _builder(root, os_name="windows", static=False)
            text = openssl._windows_cmake_package_text(builder, source)

        self.assertIn("add_library(OpenSSL::Crypto SHARED IMPORTED)", text)
        self.assertIn('IMPORTED_IMPLIB_DEBUG "${_openssl_prefix}/lib/libcryptod.lib"', text)
        self.assertIn('IMPORTED_LOCATION_RELEASE "${_openssl_prefix}/bin/libcrypto-4-x64.dll"', text)
        self.assertIn('IMPORTED_LOCATION_DEBUG "${_openssl_prefix}/bin/libcrypto-4-x64d.dll"', text)
        self.assertIn('OPENSSL_MODULES_DIR_DEBUG "${_openssl_prefix}/Debug/lib/ossl-modules"', text)
        self.assertIn("OpenSSL::applink", text)

    def test_windows_build_unsets_flags_that_override_openssl_target_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "src"
            source.mkdir()
            (source / "Configure").touch()
            builder = _builder(root, os_name="windows", static=False, dry_run=True)
            builder._jobs = lambda: 1
            builder._repo_log_path = lambda *parts: root.joinpath(*parts)
            ctx = _context(root, "Debug")
            inherited = {name: "inherited" for name in openssl._WINDOWS_CONFIGURE_FLAG_ENV}

            with patch.object(openssl, "run") as run_mock:
                openssl._run_source_build(builder, ctx, inherited)

        self.assertEqual(run_mock.call_count, 3)
        for call in run_mock.call_args_list:
            for name in openssl._WINDOWS_CONFIGURE_FLAG_ENV:
                self.assertNotIn(name, call.kwargs["env"])
            self.assertEqual(
                tuple(call.kwargs["unset_env"]),
                openssl._WINDOWS_CONFIGURE_FLAG_ENV,
            )

    def test_posix_dynamic_install_removes_only_openssl_static_archives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lib_dir = root / "prefix" / "lib"
            lib_dir.mkdir(parents=True)
            for name in ("libcrypto.a", "libssl.a", "libcrypto.so", "libdependency.a"):
                (lib_dir / name).touch()
            builder = _builder(root, os_name="linux", static=False, dry_run=False)
            openssl._remove_posix_static_archives(builder, root / "prefix")

            self.assertFalse((lib_dir / "libcrypto.a").exists())
            self.assertFalse((lib_dir / "libssl.a").exists())
            self.assertTrue((lib_dir / "libcrypto.so").exists())
            self.assertTrue((lib_dir / "libdependency.a").exists())


if __name__ == "__main__":
    unittest.main()
