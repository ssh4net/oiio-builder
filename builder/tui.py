from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import inspect
import sys
import traceback

from .config import Config, _expand_path
from .core import Builder
from .git_ops import ensure_repo
from .platform import PlatformInfo
from .preflight import run_preflight


@dataclass(frozen=True)
class TuiResult:
    action: str  # build|preflight|list-repos|print-prefixes|quit
    build_types: list[str]
    only: set[str]
    skip: set[str]
    dry_run: bool
    no_update: bool
    force: bool
    force_all: bool
    no_ffmpeg: bool


def run_tui(config: Config, platform: PlatformInfo, config_path: Path) -> int:
    if not sys.stdin.isatty():
        print("error: --tui-dialog requires an interactive terminal (TTY).", flush=True)
        return 2

    try:
        from prompt_toolkit.shortcuts import (
            checkboxlist_dialog,
            input_dialog,
            message_dialog,
            radiolist_dialog,
            yes_no_dialog,
        )
        from prompt_toolkit.styles import Style
    except ImportError:
        print(
            "error: prompt_toolkit is required for --tui-dialog.\n"
            "Install it with:\n"
            "  uv pip install prompt_toolkit",
            flush=True,
        )
        return 2

    grayscale_style = Style.from_dict(
        {
            "dialog": "bg:#202020 #d0d0d0",
            "dialog frame.label": "bg:#202020 #b0b0b0",
            "dialog.body": "bg:#202020 #d0d0d0",
            "dialog shadow": "bg:#000000",
            "frame.border": "#606060",
            "button": "bg:#303030 #d0d0d0",
            "button.focused": "bg:#606060 #ffffff",
            "checkbox": "#d0d0d0",
            "checkbox-selected": "#ffffff",
            "radiolist": "#d0d0d0",
            "radiolist-selected": "#ffffff",
            "text-area": "bg:#202020 #d0d0d0",
        }
    )

    def _dialog(factory, **kwargs):
        try:
            if "style" in inspect.signature(factory).parameters:
                kwargs["style"] = grayscale_style
        except (TypeError, ValueError):
            pass
        return factory(**kwargs)

    def _pick_yes_no(title: str, text: str) -> bool:
        return bool(_dialog(yes_no_dialog, title=title, text=text).run())

    def _pause_after_output(exit_code: int = 0) -> int | None:
        """Avoid immediately re-opening the full-screen UI after printing lots of output.

        Returning None means "continue in TUI"; returning an int means "exit TUI with this code".
        """
        try:
            choice = input("Press Enter to return to the TUI menu, or type 'q' to quit: ").strip().lower()
        except EOFError:
            return None
        if choice in {"q", "quit", "exit"}:
            return int(exit_code)
        return None

    def _pick_action() -> str:
        value = (
            _dialog(
                radiolist_dialog,
                title="oiio-builder",
                text="Select an action:",
                values=[
                    ("configure", "Configure"),
                    ("build", "Build"),
                    ("preflight", "Preflight report"),
                    ("clone", "Clone missing repos"),
                    ("update", "Update repos (fetch/pull)"),
                    ("list-repos", "List repos (print to terminal)"),
                    ("print-prefixes", "Print prefixes (print to terminal)"),
                    ("quit", "Quit"),
                ],
            ).run()
            or "quit"
        )
        return str(value)

    def _checkboxlist(
        title: str, text: str, values: list[tuple[str, str]], default_values: set[str] | None = None
    ) -> set[str] | None:
        kwargs = {"title": title, "text": text, "values": values}
        if default_values is not None:
            try:
                sig = inspect.signature(checkboxlist_dialog)
                if "default_values" in sig.parameters:
                    kwargs["default_values"] = list(sorted(default_values))
            except (TypeError, ValueError):
                # Best-effort: older prompt_toolkit versions may not support defaults.
                pass
        checked = _dialog(checkboxlist_dialog, **kwargs).run()
        if checked is None:
            return None
        return {str(v) for v in checked}

    def _probe_repos(no_update: bool) -> Builder | None:
        saved_only = set(config.only)
        saved_skip = set(config.skip)
        config.only = set()
        config.skip = set()
        try:
            return Builder(config, platform, dry_run=True, no_update=no_update, force=False)
        except SystemExit as e:
            _dialog(message_dialog, title="Error", text=str(e)).run()
            return None
        finally:
            config.only = saved_only
            config.skip = saved_skip

    def _repo_label(probe: Builder, name: str, found: bool, note: str = "") -> str:
        status = "found" if found else "missed"
        out = f"[{status}] {name}"
        if note:
            out = f"{out} {note}"
        return out

    def _pick_roots(no_update: bool) -> set[str] | None:
        probe = _probe_repos(no_update=no_update)
        if not probe:
            return None
        values: list[tuple[str, str]] = []
        for repo in probe.repos:
            if repo.name == "libiconv" and platform.os == "windows":
                zip_path = probe._libiconv_export_zip()
                found = zip_path.exists()
                label = _repo_label(probe, repo.name, found, f"(zip: {zip_path.name})")
            else:
                repo_dir = probe._resolve_repo_dir(repo)
                found = repo_dir.exists()
                label = _repo_label(probe, repo.name, found)
            values.append((repo.name, label))

        default_roots = set(config.only) if config.only else {r.name for r in probe.repos if r.name not in config.skip}
        return _checkboxlist(
            title="Root repos",
            text="Select root repos to build (deps are added automatically).\n"
            "Tip: leave empty to build all enabled repos.",
            values=values,
            default_values=default_roots,
        )

    def _pick_skip(no_update: bool) -> set[str] | None:
        probe = _probe_repos(no_update=no_update)
        if not probe:
            return None
        values: list[tuple[str, str]] = []
        for repo in probe.repos:
            if repo.name == "libiconv" and platform.os == "windows":
                zip_path = probe._libiconv_export_zip()
                found = zip_path.exists()
                label = _repo_label(probe, repo.name, found, f"(zip: {zip_path.name})")
            else:
                repo_dir = probe._resolve_repo_dir(repo)
                found = repo_dir.exists()
                label = _repo_label(probe, repo.name, found)
            values.append((repo.name, label))

        return _checkboxlist(
            title="Skip repos",
            text="Select repos to skip for this run.",
            values=values,
            default_values=set(config.skip),
        )

    def _pick_build_types() -> list[str] | None:
        values = [("Debug", "Debug"), ("Release", "Release"), ("ASAN", "ASAN")]
        current_set = {d for d in config.build_types if d in {"Debug", "Release", "ASAN"}}
        checked = _checkboxlist(
            title="Build types",
            text=f"Select build types (current: {', '.join(sorted(current_set)) or 'none'}):",
            values=values,
            default_values=current_set,
        )
        if checked is None:
            return None
        selected = [v for v in ("Debug", "Release", "ASAN") if v in checked]
        return selected or None

    def _pick_force_mode() -> tuple[bool, bool] | None:
        mode = _dialog(
            radiolist_dialog,
            title="Rebuild policy",
            text="Select rebuild policy:",
            values=[
                ("normal", "Normal (use stamps)"),
                ("force", "Force selected repos (equivalent to --force)"),
                ("force-all", "Force all repos in run (equivalent to --force-all)"),
            ],
        ).run()
        if not mode:
            return None
        if mode == "force-all":
            return False, True
        if mode == "force":
            return True, False
        return False, False

    def _pick_windows_build_settings() -> None:
        win_cfg = config.global_cfg.windows
        current_gen = str(win_cfg.get("generator", "ninja-msvc"))
        gen = _dialog(
            radiolist_dialog,
            title="Windows generator",
            text=f"Select generator (current: {current_gen}):",
            values=[
                ("ninja-msvc", "ninja-msvc (Ninja + MSVC)"),
                ("msvc", "msvc (Visual Studio 17 2022)"),
                ("msvc-clang-cl", "msvc-clang-cl (VS generator + clang-cl)"),
                ("ninja-clang-cl", "ninja-clang-cl (Ninja + clang-cl)"),
            ],
        ).run()
        if gen:
            win_cfg["generator"] = str(gen)

        current_rt = str(win_cfg.get("msvc_runtime", "static"))
        rt = _dialog(
            radiolist_dialog,
            title="MSVC runtime",
            text=f"Select MSVC runtime (current: {current_rt}):",
            values=[
                ("static", "static (/MT, /MTd)"),
                ("dynamic", "dynamic (/MD, /MDd)"),
            ],
        ).run()
        if rt:
            win_cfg["msvc_runtime"] = str(rt)

        current_wrappers = str(win_cfg.get("python_wrappers", "auto"))
        wrappers = _dialog(
            radiolist_dialog,
            title="Python wrappers",
            text=f"OpenColorIO/OpenEXR python wrappers (current: {current_wrappers}):",
            values=[
                ("auto", "auto (enabled only for dynamic runtime)"),
                ("on", "on (force enabled)"),
                ("off", "off (force disabled)"),
            ],
        ).run()
        if wrappers:
            win_cfg["python_wrappers"] = str(wrappers)

    dry_run = False
    force = False
    force_all = False
    no_update = bool(config.global_cfg.no_update)
    no_ffmpeg = False

    if platform.os == "windows":
        original_build_ffmpeg = bool(config.global_cfg.build_ffmpeg)
        original_windows_build_ffmpeg = config.global_cfg.windows.get("build_ffmpeg")
        no_ffmpeg = not original_build_ffmpeg
        if original_windows_build_ffmpeg is not None:
            if isinstance(original_windows_build_ffmpeg, str):
                value = original_windows_build_ffmpeg.strip().lower()
                if value in {"0", "false", "off", "no"}:
                    no_ffmpeg = True
                elif value in {"1", "true", "on", "yes"}:
                    no_ffmpeg = False
                else:
                    no_ffmpeg = not bool(original_windows_build_ffmpeg)
            else:
                no_ffmpeg = not bool(original_windows_build_ffmpeg)
    else:
        original_build_ffmpeg = bool(config.global_cfg.build_ffmpeg)
        original_windows_build_ffmpeg = None

    def _configure() -> None:
        nonlocal dry_run, force, force_all, no_update, no_ffmpeg

        while True:
            roots_note = ", ".join(sorted(config.only)) if config.only else "(all enabled)"
            skip_note = ", ".join(sorted(config.skip)) if config.skip else "(none)"
            choice = _dialog(
                radiolist_dialog,
                title="Configure",
                text=(
                    f"Roots: {roots_note}\n"
                    f"Skip: {skip_note}\n"
                    f"Build types: {', '.join(config.build_types)}\n"
                    f"Jobs: {config.global_cfg.jobs}\n"
                    f"Dry run: {'ON' if dry_run else 'OFF'}\n"
                    f"Update repos: {'ON' if not no_update else 'OFF'}\n"
                    f"Force: {'ON' if force else 'OFF'}\n"
                    f"Force all: {'ON' if force_all else 'OFF'}\n"
                    f"FFmpeg disabled: {'ON' if no_ffmpeg else 'OFF'}\n\n"
                    "Select what to edit:"
                ),
                values=[
                    ("roots", "Root repos (--only)"),
                    ("skip", "Skip repos (--skip)"),
                    ("build-types", "Build types"),
                    ("jobs", "Parallelism (-j)"),
                    ("paths", "Paths"),
                    ("toggles", "Toggles"),
                    ("windows", "Windows toolchain settings"),
                    ("back", "Back"),
                ],
            ).run()
            if not choice or choice == "back":
                return
            if choice == "roots":
                roots = _pick_roots(no_update=no_update)
                if roots is None:
                    continue
                config.only = set(roots)
                continue
            if choice == "skip":
                skip = _pick_skip(no_update=no_update)
                if skip is None:
                    continue
                config.skip = set(skip)
                continue
            if choice == "build-types":
                build_types = _pick_build_types()
                if build_types is None:
                    continue
                if not build_types:
                    _dialog(message_dialog, title="Error", text="No build types selected.").run()
                    continue
                config.build_types = list(build_types)
                continue
            if choice == "jobs":
                jobs_text = _dialog(
                    input_dialog,
                    title="Parallelism",
                    text=f"Max parallel jobs (0 = auto). Current: {config.global_cfg.jobs}",
                    default=str(config.global_cfg.jobs),
                ).run()
                if jobs_text is None:
                    continue
                try:
                    config.global_cfg.jobs = int(str(jobs_text).strip() or "0")
                except ValueError:
                    _dialog(message_dialog, title="Error", text=f"Invalid jobs value: {jobs_text!r}").run()
                continue
            if choice == "paths":
                src_text = _dialog(
                    input_dialog,
                    title="Paths",
                    text=f"src_root (current: {config.global_cfg.src_root})",
                    default=str(config.global_cfg.src_root),
                ).run()
                if src_text is not None:
                    config.global_cfg.src_root = _expand_path(str(src_text), config.global_cfg.repo_root)

                build_text = _dialog(
                    input_dialog,
                    title="Paths",
                    text=f"build_root (current: {config.global_cfg.build_root})",
                    default=str(config.global_cfg.build_root),
                ).run()
                if build_text is not None:
                    config.global_cfg.build_root = _expand_path(str(build_text), config.global_cfg.repo_root)

                prefix_text = _dialog(
                    input_dialog,
                    title="Paths",
                    text=f"prefix_base (empty = default). Current: {config.global_cfg.prefix_base or '(default)'}",
                    default=str(config.global_cfg.prefix_base or ""),
                ).run()
                if prefix_text is not None:
                    trimmed = str(prefix_text).strip()
                    config.global_cfg.prefix_base = trimmed or None

                if platform.os == "windows":
                    win_cfg = config.global_cfg.windows
                    install_text = _dialog(
                        input_dialog,
                        title="Paths",
                        text=f"windows.install_prefix (empty = unset). Current: {win_cfg.get('install_prefix') or '(unset)'}",
                        default=str(win_cfg.get("install_prefix") or ""),
                    ).run()
                    if install_text is not None:
                        trimmed = str(install_text).strip()
                        if trimmed:
                            win_cfg["install_prefix"] = trimmed
                        else:
                            win_cfg.pop("install_prefix", None)

                    asan_text = _dialog(
                        input_dialog,
                        title="Paths",
                        text=f"windows.asan_prefix (empty = unset). Current: {win_cfg.get('asan_prefix') or '(unset)'}",
                        default=str(win_cfg.get("asan_prefix") or ""),
                    ).run()
                    if asan_text is not None:
                        trimmed = str(asan_text).strip()
                        if trimmed:
                            win_cfg["asan_prefix"] = trimmed
                        else:
                            win_cfg.pop("asan_prefix", None)
                continue
            if choice == "toggles":
                dry_run = _pick_yes_no("Dry run", f"Dry run (print commands only)?\nCurrent: {'ON' if dry_run else 'OFF'}")

                update_now = _pick_yes_no(
                    "Git update",
                    f"Update repos (git fetch/pull) on build?\nCurrent: {'OFF' if no_update else 'ON'}",
                )
                no_update = not update_now

                fm = _pick_force_mode()
                if fm is not None:
                    force, force_all = fm

                if platform.os == "windows":
                    no_ffmpeg = _pick_yes_no(
                        "FFmpeg",
                        f"Disable FFmpeg (OpenImageIO ffmpeg plugin detection)?\nCurrent: {'ON' if no_ffmpeg else 'OFF'}",
                    )
                    if no_ffmpeg:
                        config.global_cfg.build_ffmpeg = False
                        config.global_cfg.windows["build_ffmpeg"] = False
                    else:
                        config.global_cfg.build_ffmpeg = original_build_ffmpeg
                        if original_windows_build_ffmpeg is None:
                            config.global_cfg.windows.pop("build_ffmpeg", None)
                        else:
                            config.global_cfg.windows["build_ffmpeg"] = original_windows_build_ffmpeg
                continue
            if choice == "windows":
                if platform.os != "windows":
                    _dialog(message_dialog, title="Windows", text="Not available on non-Windows.").run()
                    continue
                _pick_windows_build_settings()
                continue

    def _builder_for_action(dry_run_flag: bool, update_flag: bool, force_flag: bool, force_all_flag: bool) -> Builder | None:
        try:
            return Builder(
                config,
                platform,
                dry_run=dry_run_flag,
                no_update=not update_flag,
                force=force_flag,
                force_all=force_all_flag,
            )
        except SystemExit as e:
            _dialog(message_dialog, title="Error", text=str(e)).run()
            return None

    while True:
        action = _pick_action()
        if action == "quit":
            return 0

        if action == "configure":
            _configure()
            continue

        if action == "preflight":
            run_preflight(config, platform, no_update=no_update)
            code = _pause_after_output(0)
            if code is not None:
                return code
            continue

        if action == "list-repos":
            builder = _builder_for_action(dry_run_flag=True, update_flag=False, force_flag=False, force_all_flag=False)
            if not builder:
                continue
            print("")
            for repo in builder.repos:
                print(repo.name)
            code = _pause_after_output(0)
            if code is not None:
                return code
            continue

        if action == "print-prefixes":
            builder = _builder_for_action(dry_run_flag=True, update_flag=False, force_flag=False, force_all_flag=False)
            if not builder:
                continue
            print("")
            for key in ("Release", "Debug", "ASAN"):
                value = builder.prefixes.get(key)
                if value:
                    print(f"{key}: {value}")
            code = _pause_after_output(0)
            if code is not None:
                return code
            continue

        if action in {"clone", "update"}:
            update_repos = action == "update"
            builder = _builder_for_action(dry_run_flag=True, update_flag=False, force_flag=False, force_all_flag=False)
            if not builder:
                continue

            prompt = "Proceed with git fetch/pull for planned repos?" if update_repos else "Proceed with cloning missing planned repos?"
            if not _pick_yes_no("Confirm", prompt):
                continue

            try:
                for repo in builder.repos:
                    if repo.name == "libiconv" and platform.os == "windows":
                        continue
                    if not repo.url:
                        continue
                    repo_dir = builder._resolve_repo_dir(repo)
                    if not update_repos and repo_dir.exists():
                        continue
                    ensure_repo(
                        repo_dir,
                        repo.url,
                        repo.ref,
                        repo.ref_type,
                        update=update_repos,
                        dry_run=dry_run,
                    )
            except Exception as e:
                _dialog(message_dialog, title="Git error", text=str(e) or e.__class__.__name__).run()
                traceback.print_exc()
                continue
            code = _pause_after_output(0)
            if code is not None:
                return code
            continue

        if action != "build":
            _dialog(message_dialog, title="Error", text=f"Unknown action: {action}").run()
            continue

        roots_note = ", ".join(sorted(config.only)) if config.only else "(all enabled repos)"
        skip_note = ", ".join(sorted(config.skip)) if config.skip else "(none)"
        summary_lines = [
            f"Config: {config_path}",
            f"Platform: {platform.os} {platform.arch}",
            f"Build types: {', '.join(config.build_types)}",
            f"Roots: {roots_note}",
            f"Skip: {skip_note}",
            f"Jobs: {config.global_cfg.jobs}",
            f"Dry run: {'ON' if dry_run else 'OFF'}",
            f"Update repos: {'ON' if not no_update else 'OFF'}",
            f"Force: {'ON' if force else 'OFF'}",
            f"Force all: {'ON' if force_all else 'OFF'}",
        ]
        if platform.os == "windows":
            summary_lines.append(f"FFmpeg disabled: {'ON' if no_ffmpeg else 'OFF'}")
            win_cfg = config.global_cfg.windows
            summary_lines.append(f"Generator: {win_cfg.get('generator', 'ninja-msvc')}")
            summary_lines.append(f"MSVC runtime: {win_cfg.get('msvc_runtime', 'static')}")
            summary_lines.append(f"Python wrappers: {win_cfg.get('python_wrappers', 'auto')}")

        if not _pick_yes_no("Confirm", "\n".join(summary_lines) + "\n\nProceed with build?"):
            continue

        builder = _builder_for_action(
            dry_run_flag=dry_run,
            update_flag=not no_update,
            force_flag=force,
            force_all_flag=force_all,
        )
        if not builder:
            continue
        try:
            rc = builder.run()
        except Exception as e:
            _dialog(message_dialog, title="Build failed", text=str(e) or e.__class__.__name__).run()
            traceback.print_exc()
            continue
        code = _pause_after_output(rc)
        if code is not None:
            return code
