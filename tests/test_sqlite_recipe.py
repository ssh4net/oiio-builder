from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from builder.recipes import sqlite


def _builder(*, static: bool, os_name: str = "windows", dry_run: bool = True):
    windows = {"debug_postfix": "d", "msvc_runtime": "static" if static else "dynamic"}
    builder = SimpleNamespace(
        config=SimpleNamespace(
            global_cfg=SimpleNamespace(
                static_default=static,
                windows=windows,
            )
        ),
        platform=SimpleNamespace(os=os_name, arch="x86_64"),
        toolchain={"cc": "cl.exe" if os_name == "windows" else "cc", "ar": "ar"},
        dry_run=dry_run,
        repo_paths={},
        _windows_runtime_mode=lambda: windows["msvc_runtime"],
        _non_cmake_flags=lambda build_type: (
            ("/Od /Zi /MTd" if static else "/Od /Zi /MDd")
            if build_type == "Debug"
            else ("/O2 /MT" if static else "/O2 /MD"),
            "",
            "",
        ),
    )
    return builder


def _context(tmp: Path, build_type: str):
    return SimpleNamespace(
        repo=SimpleNamespace(name="sqlite"),
        build_type=build_type,
        build_dir=tmp / "build",
        install_prefix=tmp / "prefix",
        src_dir=tmp / "src",
    )


class SQLiteRecipeTests(unittest.TestCase):
    def test_sqlite_is_not_tied_to_cpython(self):
        self.assertTrue(sqlite.enabled(None, None))

    def test_static_zipfile_header_does_not_remap_sqlite_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            header = Path(tmp) / "sqlite3_zipfile.h"
            sqlite._write_zipfile_header(header)
            text = header.read_text(encoding="utf-8")

        self.assertIn("#include <sqlite3.h>", text)
        self.assertNotIn("#include <sqlite3ext.h>", text)

    def test_posix_source_rejects_crlf_generators_early(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp)
            generator = source / "tool" / "mksqlite3c.tcl"
            generator.parent.mkdir()
            generator.write_bytes(b"set value 1\r\nputs $value\r\n")

            with self.assertRaisesRegex(RuntimeError, "core.autocrlf=false"):
                sqlite._validate_posix_source_line_endings(source, dry_run=False)

    def test_posix_features_are_explicit_and_do_not_require_tcl_or_icu(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Release")
            args = sqlite._posix_make_arguments(_builder(static=True, os_name="linux"), ctx)

        features = next(arg for arg in args if arg.startswith("OPT_FEATURE_FLAGS="))
        self.assertIn("SQLITE_ENABLE_FTS5", features)
        self.assertIn("SQLITE_ENABLE_RTREE", features)
        self.assertIn("SQLITE_ENABLE_GEOPOLY", features)
        self.assertIn("HAVE_TCL=0", args)
        self.assertIn("ENABLE_LIB_STATIC=1", args)
        self.assertIn("ENABLE_LIB_SHARED=0", args)
        self.assertFalse(any("icu" in arg.lower() for arg in args))

    def test_macos_dynamic_uses_dylib_install_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Release")
            args = sqlite._posix_make_arguments(_builder(static=False, os_name="macos"), ctx)

        self.assertIn("ENABLE_LIB_STATIC=0", args)
        self.assertIn("ENABLE_LIB_SHARED=1", args)
        self.assertIn("T.dll=.dylib", args)
        self.assertIn("LDFLAGS.shlib=-dynamiclib", args)
        self.assertIn("libsqlite3.DLL.install-rules=darwin", args)
        self.assertIn("LDFLAGS.dlopen=", args)

    def test_windows_static_debug_uses_static_crt_and_zlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Debug")
            command = sqlite._windows_nmake_command(_builder(static=True), ctx, {})

        self.assertIn("libsqlite3.lib", command)
        self.assertIn("sqlite3d.exe", command)
        self.assertIn("USE_CRT_DLL=0", command)
        self.assertIn("DYNAMIC_SHELL=0", command)
        self.assertIn("DEBUG=2", command)
        self.assertIn("ZLIBLIB=zlibstaticd.lib", command)
        self.assertIn("NO_TCL=1", command)
        options = next(arg for arg in command if arg.startswith("OPTS="))
        for feature in ("SQLITE_ENABLE_FTS5", "SQLITE_ENABLE_RTREE", "SQLITE_ENABLE_GEOPOLY"):
            self.assertIn(feature, options)
        self.assertNotIn("SQLITE_ENABLE_JSON1", options)

    def test_windows_dynamic_release_uses_dll_crt_and_shared_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Release")
            command = sqlite._windows_nmake_command(_builder(static=False), ctx, {})

        self.assertIn("sqlite3.dll", command)
        self.assertIn("sqlite3.exe", command)
        self.assertIn("USE_CRT_DLL=1", command)
        self.assertIn("LDFLAGS=/DEBUG", command)
        self.assertIn("DYNAMIC_SHELL=1", command)
        self.assertIn("DEBUG=0", command)
        self.assertIn("ZLIBLIB=zlib.lib", command)
        self.assertNotIn("USE_NATIVE_LIBPATHS=1", command)

    def test_windows_asan_uses_release_names_and_asan_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "ASAN")
            command = sqlite._windows_nmake_command(_builder(static=False), ctx, {})

        self.assertIn("sqlite3.dll", command)
        self.assertIn("sqlite3.exe", command)
        self.assertIn("DEBUG=0", command)
        self.assertIn("ASAN=1", command)

    def test_windows_bootstrap_uses_active_msvc_compatible_compiler(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(Path(tmp), "Release")
            builder = _builder(static=False)
            builder.toolchain["cc"] = "clang-cl.exe"
            command = sqlite._windows_jimsh_command(builder, ctx)

        self.assertEqual(command[0], "clang-cl.exe")
        self.assertIn("/DHAVE__FULLPATH=1", command)
        self.assertTrue(command[-1].endswith("jimsh0.exe"))

    def test_generated_windows_package_describes_both_configurations(self):
        static_text = sqlite._cmake_package_text(_builder(static=True), "Debug", "3.50.0")
        self.assertIn("add_library(SQLite::SQLite3 STATIC IMPORTED)", static_text)
        self.assertIn("IMPORTED_LOCATION_DEBUG \"${_sqlite_prefix}/lib/sqlite3d.lib\"", static_text)
        self.assertIn("add_library(SQLite::Zipfile STATIC IMPORTED)", static_text)
        self.assertIn("ZLIB::ZLIB", static_text)

        posix_static_text = sqlite._cmake_package_text(
            _builder(static=True, os_name="linux"), "Release", "3.50.0"
        )
        self.assertIn("find_dependency(Threads REQUIRED)", posix_static_text)
        self.assertIn("Threads::Threads;${CMAKE_DL_LIBS};m", posix_static_text)

        shared_text = sqlite._cmake_package_text(_builder(static=False), "Release", "3.50.0")
        self.assertIn("add_library(SQLite::SQLite3 SHARED IMPORTED)", shared_text)
        self.assertIn("IMPORTED_IMPLIB_DEBUG \"${_sqlite_prefix}/lib/sqlite3d.lib\"", shared_text)
        self.assertIn("IMPORTED_LOCATION_RELEASE \"${_sqlite_prefix}/bin/sqlite3.dll\"", shared_text)
        self.assertIn("add_library(SQLite::Zipfile MODULE IMPORTED)", shared_text)
        self.assertIn("add_library(SQLite3::SQLite3 INTERFACE IMPORTED)", shared_text)

        debug_shared_text = sqlite._cmake_package_text(_builder(static=False), "Debug", "3.50.0")
        self.assertIn(
            'set(SQLite3_ZIPFILE_EXTENSION "${_sqlite_prefix}/bin/zipfiled.dll")',
            debug_shared_text,
        )

    def test_windows_debug_pkgconfig_uses_debug_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            (source / "VERSION").write_text("3.50.0\n", encoding="utf-8")
            builder = _builder(static=False, dry_run=False)
            builder.repo_paths = {"sqlite": source}

            sqlite._write_package_files(builder, root / "prefix", "Debug")
            pkgconfig = (root / "prefix/lib/pkgconfig/sqlite3.pc").read_text(encoding="utf-8")

        self.assertIn("Libs: -L${libdir} -lsqlite3d", pkgconfig)

    def test_generated_package_configures_in_an_independent_consumer(self):
        cmake = shutil.which("cmake")
        if cmake is None:
            self.skipTest("cmake is required for the package probe")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefix = root / "prefix"
            sqlite_dir = prefix / "lib" / "cmake" / "SQLite3"
            zlib_dir = prefix / "lib" / "cmake" / "ZLIB"
            sqlite_dir.mkdir(parents=True)
            zlib_dir.mkdir(parents=True)
            (sqlite_dir / "SQLite3Config.cmake").write_text(
                sqlite._cmake_package_text(_builder(static=True), "Release", "3.50.0"),
                encoding="utf-8",
            )
            (zlib_dir / "ZLIBConfig.cmake").write_text(
                "add_library(ZLIB::ZLIB STATIC IMPORTED)\nset(ZLIB_FOUND TRUE)\n",
                encoding="utf-8",
            )

            source_dir = root / "consumer"
            source_dir.mkdir()
            (source_dir / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(sqlite_package_probe C)\n"
                "set(CMAKE_FIND_PACKAGE_PREFER_CONFIG TRUE)\n"
                "find_package(SQLite3 CONFIG REQUIRED)\n"
                "foreach(target SQLite::SQLite3 SQLite3::SQLite3 SQLite::Zipfile)\n"
                "  if(NOT TARGET ${target})\n"
                "    message(FATAL_ERROR \"missing target: ${target}\")\n"
                "  endif()\n"
                "endforeach()\n",
                encoding="utf-8",
            )
            subprocess.run(
                [cmake, "-S", str(source_dir), "-B", str(root / "build"), f"-DCMAKE_PREFIX_PATH={prefix}"],
                check=True,
                capture_output=True,
                text=True,
            )


if __name__ == "__main__":
    unittest.main()
