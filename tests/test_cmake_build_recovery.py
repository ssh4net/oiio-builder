from __future__ import annotations

import subprocess
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from builder import backends


TRANSIENT = (
    "FAILED: simple_dcraw \n"
    "ld.lld: error: failed to write output 'simple_dcraw': Resource temporarily unavailable\n"
    "clang++-20: error: linker command failed with exit code 1 (use -v to see invocation)\n"
    "ninja: build stopped: subcommand failed.\n"
)


def _context(root: Path):
    log = root / "logs" / "build.log"
    log.parent.mkdir()
    builder = SimpleNamespace(dry_run=False, _repo_log_path=lambda *_: log, _jobs=lambda: 32)
    ctx = SimpleNamespace(repo=SimpleNamespace(name="libraw", cmake_args=[]), build_type="Debug",
                          build_dir=root / "build", src_dir=root / "source", install_prefix=root / "install")
    command = ["cmake", "--build", str(ctx.build_dir), "--config", "Debug", "--parallel", "32"]
    return builder, ctx, command, log


class CmakeBuildRecoveryTests(unittest.TestCase):
    def test_only_recognized_temporary_output_errors_are_retryable(self):
        cases = (
            (TRANSIENT, True),
            (TRANSIENT + TRANSIENT.replace("simple_dcraw", "raw-identify"), True),
            ("ld.lld-20: error: failed to write output 'path with spaces': Resource temporarily unavailable\n", True),
            (TRANSIENT.replace("Resource temporarily unavailable", "Permission denied"), False),
            (TRANSIENT + "source.cpp:42: error: undeclared identifier\n", False),
            (TRANSIENT + "ld.lld: error: undefined symbol: missing_function\n", False),
            (TRANSIENT + "CMake Error: script failed\n", False),
            (TRANSIENT + "FAILED: another_target\nSegmentation fault\n", False),
            ("clang++-20: error: linker command failed with exit code 1 (use -v to see invocation)\n", False),
            ("", False),
        )
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "build.log"
            for text, expected in cases:
                with self.subTest(text=text):
                    log.write_text(text)
                    self.assertEqual(expected, backends._transient_lld_output_failure(log))
            self.assertFalse(backends._transient_lld_output_failure(Path(tmp) / "missing.log"))

    def test_retry_preserves_first_log_and_uses_same_command_and_build_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder, ctx, command, log = _context(Path(tmp))
            ctx.build_dir.mkdir()
            object_file = ctx.build_dir / "compiled.o"
            object_file.write_bytes(b"compiled object")
            previous = log.with_name("build.first-attempt.previous.log")
            previous.write_text("older evidence")
            def run(*args, **kwargs):
                self.assertEqual((command,), args)
                self.assertEqual(str(log), kwargs["log_path"])
                self.assertEqual({"TEST_ENV": "present"}, kwargs["env"])
                if not log.exists():
                    log.write_text(TRANSIENT)
                    raise subprocess.CalledProcessError(1, command)
                log.write_text("build passed\n")
            with patch.object(backends, "run", side_effect=run) as runner:
                backends._run_cmake_build(builder, ctx, {"TEST_ENV": "present"}, command)
            self.assertEqual(2, runner.call_count)
            self.assertEqual("build passed\n", log.read_text())
            backups = [p for p in log.parent.glob("build.first-attempt.*.log") if p != previous]
            self.assertEqual(1, len(backups))
            self.assertEqual(TRANSIENT, backups[0].read_text())
            self.assertEqual("older evidence", previous.read_text())
            self.assertEqual(b"compiled object", object_file.read_bytes())

    def test_failed_second_attempt_is_propagated_without_a_third_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder, ctx, command, log = _context(Path(tmp))
            second_error = subprocess.CalledProcessError(2, command)
            def run(*_args, **_kwargs):
                if log.exists():
                    log.write_text("second build failed\n")
                    raise second_error
                log.write_text(TRANSIENT)
                raise subprocess.CalledProcessError(1, command)
            with patch.object(backends, "run", side_effect=run) as runner:
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    backends._run_cmake_build(builder, ctx, {}, command)
            self.assertIs(second_error, raised.exception)
            self.assertEqual(2, runner.call_count)
            self.assertEqual(TRANSIENT, next(log.parent.glob("build.first-attempt.*.log")).read_text())

    def test_unrelated_or_missing_failure_log_does_not_retry(self):
        for diagnostic in (None, "compiler.cpp:3: error: unknown type\n", TRANSIENT + "ld.lld: error: undefined symbol: x\n"):
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as tmp:
                builder, ctx, command, log = _context(Path(tmp))
                if diagnostic is not None:
                    log.write_text(diagnostic)
                failure = subprocess.CalledProcessError(1, command)
                with patch.object(backends, "run", side_effect=failure) as runner:
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        backends._run_cmake_build(builder, ctx, {}, command)
                self.assertIs(failure, raised.exception)
                self.assertEqual(1, runner.call_count)
                self.assertEqual([], list(log.parent.glob("build.first-attempt.*.log")))

    def test_backup_failure_preserves_original_error_and_log_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder, ctx, command, log = _context(Path(tmp))
            log.write_text(TRANSIENT)
            failure = subprocess.CalledProcessError(1, command)
            with patch.object(backends, "run", side_effect=failure) as runner, \
                    patch.object(backends.shutil, "copy2", side_effect=OSError("cannot save log")):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    backends._run_cmake_build(builder, ctx, {}, command)
            self.assertIs(failure, raised.exception)
            self.assertEqual(1, runner.call_count)
            self.assertEqual(TRANSIENT, log.read_text())
            self.assertEqual([], list(log.parent.glob("build.first-attempt.*.log")))

    def test_configure_and_install_failures_do_not_retry(self):
        for phase in ("configure", "install"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                builder, ctx, _, log = _context(Path(tmp))
                builder._repo_cmake_user_override_args = lambda *_: []
                builder._license_profile_cmake_args = lambda *_: []
                log.write_text(TRANSIENT)
                failure = subprocess.CalledProcessError(1, ["cmake"])
                def run(command, **_kwargs):
                    if (phase == "configure" and "-S" in command) or (phase == "install" and "--install" in command):
                        raise failure
                with patch.object(backends, "run", side_effect=run) as runner, \
                        patch.object(backends, "cmake_generator_args", return_value=[]), \
                        patch.object(backends, "cmake_common_args", return_value=[]), \
                        patch.object(backends, "_expand_args", return_value=[]), \
                        patch.object(backends, "_repo_specific_args", return_value=[]):
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        backends._build_cmake(builder, ctx, {})
                self.assertIs(failure, raised.exception)
                self.assertEqual(1 if phase == "configure" else 3, runner.call_count)
                self.assertEqual([], list(log.parent.glob("build.first-attempt.*.log")))

    @unittest.skipUnless(shutil.which("cmake") and shutil.which("cc") and shutil.which("ninja"),
                         "CMake, Ninja and a C compiler are needed for build recovery")
    def test_real_cmake_graph_resumes_after_injected_temporary_write_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            builder, ctx, command, log = _context(Path(tmp))
            ctx.src_dir.mkdir()
            (ctx.src_dir / "main.c").write_text("int main(void) { return 0; }\n")
            (ctx.src_dir / "fail_once.py").write_text(
                "from pathlib import Path\nimport sys\nmarker = Path('first-link-attempt')\n"
                "if not marker.exists():\n    marker.touch()\n"
                "    print(\"ld.lld: error: failed to write output 'consumer': Resource temporarily unavailable\", file=sys.stderr)\n"
                "    raise SystemExit(1)\n"
            )
            (ctx.src_dir / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.14)\nproject(recovery_fixture C)\n"
                "add_executable(consumer main.c)\n"
                "add_custom_command(TARGET consumer PRE_LINK\n"
                '  COMMAND "${TEST_PYTHON}" "${CMAKE_CURRENT_SOURCE_DIR}/fail_once.py" VERBATIM)\n'
            )
            configure = subprocess.run(
                ["cmake", "-S", str(ctx.src_dir), "-B", str(ctx.build_dir), "-G", "Ninja",
                 f"-DTEST_PYTHON={sys.executable}"], capture_output=True, text=True,
            )
            self.assertEqual(0, configure.returncode, configure.stdout + configure.stderr)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                backends._run_cmake_build(builder, ctx, {}, command)
            saved = next(log.parent.glob("build.first-attempt.*.log"))
            self.assertIn("Resource temporarily unavailable", saved.read_text())
            self.assertIn("Linking C executable consumer", log.read_text())
            self.assertNotIn("Building C object", log.read_text())
            executable = next(p for p in (ctx.build_dir / "consumer", ctx.build_dir / "consumer.exe") if p.is_file())
            self.assertEqual(0, subprocess.run([str(executable)]).returncode)


if __name__ == "__main__":
    unittest.main()
