from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from builder import cli


class ForceUpdateCliTests(unittest.TestCase):
    def _configs(self, root: Path) -> tuple[Path, Path]:
        config = root / "build.toml"
        config.write_text(
            '[global]\nsrc_root = "default-sources"\n'
            '[[repos]]\nname = "fixture"\ndir = "fixture"\n', encoding="utf-8",
        )
        (root / "build.user.toml").write_text(
            '[global]\nsrc_root = "auto-sources"\n', encoding="utf-8",
        )
        user = root / "build_ub_ngpl.user.toml"
        user.write_text(
            '[global]\nsrc_root = "selected-sources"\nprofile = "nongpl-static"\n', encoding="utf-8",
        )
        return config, user

    def test_configuration_selectors_accept_separate_equals_and_abbreviated_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, user = self._configs(root)
            cases = [
                (["--force-update", "--config", str(config)], "auto-sources", None),
                (["--force-update", "--config", str(config), "--user-config", str(user)],
                 "selected-sources", "nongpl-static"),
                ([f"--config={config}", f"--user-config={user}", "--force-update"],
                 "selected-sources", "nongpl-static"),
                (["--force-up", "--con", str(config), "--user-c", str(user)],
                 "selected-sources", "nongpl-static"),
                (["--force-update", "--config", str(config), "--no-user-config"], "default-sources", None),
            ]
            for arguments, source, profile in cases:
                with self.subTest(arguments=arguments), patch("sys.argv", ["build.py", *arguments]), \
                     patch.object(cli, "_run_force_update", return_value=23) as refresh, \
                     patch.object(cli, "Builder", side_effect=AssertionError("must not build")):
                    self.assertEqual(23, cli.main())
                    selected = refresh.call_args.args[0]
                    self.assertEqual(root / source, selected.global_cfg.src_root)
                    self.assertEqual(profile, selected.global_cfg.profile)

    def test_operational_options_are_rejected_including_falsey_values(self):
        operations = [
            ["--build-types", "Debug"], ["--only", "fixture"], ["--skip", "fixture"],
            ["--no-update"], ["--update"], ["--update-only"], ["--prepare-only"],
            ["--apply-prefix-contract"], ["--dry-run"], ["--no-ffmpeg"], ["--force"],
            ["--force-all"], ["--reinstall"], ["--reinstall-all"], ["--parallel-build-types"],
            ["--no-ccache"], ["--preflight"], ["--list-repos"], ["--print-prefixes"],
            ["--register-sources"], ["--profile", "nongpl-static"], ["--jobs", "0"],
            ["--only", ""], ["--skip", ""], ["--build-types", ""], ["--profile", ""],
        ]
        for arguments in operations:
            with self.subTest(arguments=arguments), \
                 patch("sys.argv", ["build.py", "--force-update", *arguments]), \
                 patch.object(cli, "load_config", side_effect=AssertionError("must reject before loading")), \
                 patch.object(cli, "_run_force_update", side_effect=AssertionError("must not refresh")), \
                 contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(SystemExit) as raised:
                    cli.main()
                self.assertEqual(2, raised.exception.code)
                self.assertIn("--force-update may be combined only", errors.getvalue())

    def test_selected_config_still_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, user = self._configs(root)
            checkout = root / "selected-sources" / "fixture"
            (checkout / ".git").mkdir(parents=True)
            with patch("sys.argv", ["build.py", "--force-update", "--config", str(config),
                                    "--user-config", str(user)]), \
                 patch("builtins.input", return_value="cancel") as confirmation, \
                 patch.object(cli, "force_update_repo") as refresh, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(1, cli.main())
                confirmation.assert_called_once_with("Type FORCE-UPDATE to continue: ")
                refresh.assert_not_called()
                self.assertIn(str(checkout), output.getvalue())
                self.assertIn("Force update cancelled", output.getvalue())

    def test_confirmed_refresh_uses_only_the_selected_source_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, user = self._configs(root)
            for source in ("default-sources", "auto-sources", "selected-sources"):
                (root / source / "fixture" / ".git").mkdir(parents=True)
            with patch("sys.argv", ["build.py", "--force-update", "--config", str(config),
                                    "--user-config", str(user)]), \
                 patch("builtins.input", return_value="FORCE-UPDATE"), \
                 patch.object(cli, "force_update_repo", return_value=True) as refresh, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli.main())
                refresh.assert_called_once_with(root / "selected-sources" / "fixture", None, None, "branch")


if __name__ == "__main__":
    unittest.main()
