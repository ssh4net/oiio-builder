from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder import backends
from builder.core import Builder
from builder.platform import PlatformInfo
from builder.stamps import compute_stamp


def _builder(platform: str = "linux", *, libcxx: bool = False) -> Builder:
    builder = object.__new__(Builder)
    builder.platform = PlatformInfo(os=platform, arch="x86_64")
    builder.config = SimpleNamespace(global_cfg=SimpleNamespace(
        pic=True, use_libcxx=libcxx, windows={"generator": "ninja-msvc"},
    ))
    builder.toolchain = {"sdkroot": "/test-sdk"} if platform == "macos" else {}
    builder.license_profile = None
    builder._toolchain_fingerprint = lambda: {}
    builder._repo_cmake_effective_toml_options = lambda name: SimpleNamespace(cache={}, args=[])
    return builder


class NonCMakeAsanTests(unittest.TestCase):
    def test_sanitizer_linkage_preserves_platform_and_abi_flags(self):
        for platform in ("linux", "macos", "windows"):
            for mode in ("Debug", "Release", "ASAN"):
                for libcxx in (False, True):
                    with self.subTest(platform=platform, mode=mode, libcxx=libcxx):
                        flags = _builder(platform, libcxx=libcxx)._non_cmake_flags(mode)
                        cflags, cxxflags, ldflags = flags
                        if platform == "windows":
                            self.assertEqual("", ldflags)
                            self.assertEqual(mode == "ASAN", "/fsanitize=address" in cflags)
                            self.assertEqual(mode == "ASAN", "/fsanitize=address" in cxxflags)
                            continue
                        for value in flags:
                            self.assertEqual(mode == "ASAN", "-fsanitize=address" in value)
                            self.assertEqual(platform == "macos", "-isysroot /test-sdk" in value)
                        self.assertIn("-fPIC", cflags)
                        self.assertIn("-fPIC", cxxflags)
                        self.assertEqual(libcxx, "-stdlib=libc++" in cxxflags)
                        self.assertEqual(libcxx, "-stdlib=libc++" in ldflags)

    def test_only_affected_asan_stamps_track_linker_flags(self):
        with patch("builder.core.git_head", return_value="source-head"):
            for platform in ("linux", "macos", "windows"):
                builder = _builder(platform)
                for mode in ("Debug", "Release", "ASAN"):
                    for system in ("autotools", "cpython", "ffmpeg", "openssl", "sqlite", "cmake"):
                        with self.subTest(platform=platform, mode=mode, system=system):
                            repo = SimpleNamespace(name="asan-probe", build_system=system, cmake_args=[])
                            ctx = SimpleNamespace(build_type=mode, src_dir=Path("source"))
                            payload = builder._stamp_payload(repo, ctx, {}, "cflags", "cxxflags")
                            tracked = (platform != "windows" and mode == "ASAN"
                                       and system in {"autotools", "cpython", "ffmpeg", "openssl"})
                            self.assertEqual(tracked, "non_cmake_ldflags" in payload)
                            if tracked:
                                with patch.object(builder, "_non_cmake_flags", return_value=("", "", "changed")):
                                    changed = builder._stamp_payload(repo, ctx, {}, "cflags", "cxxflags")
                                self.assertNotEqual(compute_stamp(payload), compute_stamp(changed))

    @unittest.skipUnless(os.name != "nt" and shutil.which("clang") and shutil.which("clang++"),
                         "Clang C and C++ drivers are needed for the split-link probe")
    def test_autotools_can_link_an_asan_object_without_reusing_cflags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builder = _builder(libcxx=True)
            builder.toolchain = {"cc": shutil.which("clang"), "cxx": shutil.which("clang++")}
            ctx = SimpleNamespace(build_type="ASAN", install_prefix=root / "prefix")
            env = backends._autotools_build_env(builder, ctx, {})
            self.assertNotIn("-stdlib=libc++", env["LDFLAGS"])
            self.assertIn("-stdlib=libc++", env["CXXFLAGS"])
            source = root / "probe.c"
            obj = root / "probe.o"
            binary = root / "probe"
            source.write_text("int values[3] = {1, 2, 3}; int main(void) { return values[0] != 1; }\n")
            compile_result = subprocess.run(
                [env["CC"], *shlex.split(env["CFLAGS"]), "-c", str(source), "-o", str(obj)],
                capture_output=True, text=True,
            )
            self.assertEqual(0, compile_result.returncode, compile_result.stderr)
            flags = shlex.split(env["LDFLAGS"])
            # libvpx's check_ld uses LDFLAGS alone after a separate C compile.
            baseline = subprocess.run(
                [env["CXX"], *(flag for flag in flags if flag != "-fsanitize=address"),
                 str(obj), "-o", str(binary)], capture_output=True, text=True,
            )
            self.assertNotEqual(0, baseline.returncode)
            self.assertIn("__asan_", baseline.stderr)
            linked = subprocess.run(
                [env["CXX"], *flags, str(obj), "-o", str(binary)], capture_output=True, text=True,
            )
            self.assertEqual(0, linked.returncode, linked.stderr)


if __name__ == "__main__":
    unittest.main()
