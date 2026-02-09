from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import sys

from .config import Config, _expand_path
from .core import Builder
from .git_ops import ensure_repo
from .platform import PlatformInfo
from .preflight import run_preflight


@dataclass
class UiState:
    # Repo selection mode:
    # - "all": build all enabled repos (except --skip)
    # - "selected": build selected roots only (deps auto-added)
    repo_mode: str = "all"
    saved_roots: set[str] = field(default_factory=set)

    dry_run: bool = False
    no_update: bool = True
    force: bool = False
    force_all: bool = False
    no_ffmpeg: bool = False
    last_plan_error: str | None = None


def _parse_bool(value: str) -> bool | None:
    v = value.strip().lower()
    if v in {"1", "true", "on", "yes", "y"}:
        return True
    if v in {"0", "false", "off", "no", "n"}:
        return False
    return None


def _resolve_repo_names(available: list[str], raw: list[str]) -> set[str]:
    by_exact = {name: name for name in available}
    by_lower: dict[str, list[str]] = {}
    for name in available:
        by_lower.setdefault(name.lower(), []).append(name)

    resolved: set[str] = set()
    unknown: list[str] = []
    ambiguous: list[tuple[str, list[str]]] = []
    for name in raw:
        if name in by_exact:
            resolved.add(name)
            continue
        matches = by_lower.get(name.lower(), [])
        if len(matches) == 1:
            resolved.add(matches[0])
        elif len(matches) > 1:
            ambiguous.append((name, matches))
        else:
            unknown.append(name)
    if ambiguous:
        items = "; ".join(f"{name} -> {', '.join(matches)}" for name, matches in ambiguous)
        raise ValueError(f"ambiguous repo name(s): {items}")
    if unknown:
        raise ValueError(f"unknown repo name(s): {', '.join(unknown)}")
    return resolved


def _prompt(text: str, default: str | None = None) -> str:
    if default is not None:
        text = f"{text} [{default}]"
    return input(f"{text}: ").strip()


def _pause() -> None:
    input("Press Enter to continue... ")


def _ffmpeg_enabled(config: Config, platform: PlatformInfo) -> bool:
    enabled = bool(config.global_cfg.build_ffmpeg)
    if platform.os != "windows":
        return enabled
    override = config.global_cfg.windows.get("build_ffmpeg")
    if override is None:
        return enabled
    if isinstance(override, str):
        value = _parse_bool(override)
        if value is not None:
            return value
    return bool(override)


def _builder_for_enabled_repos(config: Config, platform: PlatformInfo, state: UiState) -> Builder:
    saved_only = set(config.only)
    saved_skip = set(config.skip)
    config.only = set()
    config.skip = set()
    try:
        return Builder(config, platform, dry_run=True, no_update=state.no_update, force=False)
    finally:
        config.only = saved_only
        config.skip = saved_skip


def _builder_for_plan(config: Config, platform: PlatformInfo, state: UiState) -> Builder | None:
    state.last_plan_error = None
    if state.repo_mode == "selected" and not config.only:
        return None
    try:
        return Builder(config, platform, dry_run=True, no_update=state.no_update, force=False)
    except SystemExit as e:
        state.last_plan_error = str(e)
        return None


def _print_main_packages(config: Config, platform: PlatformInfo, state: UiState) -> None:
    enabled = _builder_for_enabled_repos(config, platform, state)
    enabled_repos = enabled.repos
    plan_builder = _builder_for_plan(config, platform, state)
    planned_names = {repo.name for repo in (plan_builder.repos if plan_builder else [])}

    print("=== Packages ===")
    if not enabled_repos:
        print("(no repos enabled by config/toggles)")
        return

    idx_width = len(str(len(enabled_repos)))
    name_width = max(len(repo.name) for repo in enabled_repos)
    print(f"Mode: {state.repo_mode}  Build types: {', '.join(config.build_types)}")
    if state.last_plan_error:
        first = state.last_plan_error.splitlines()[0]
        print(f"Plan error: {first}")
    print(f"{'#':>{idx_width}}  {'Plan':<3}  {'Src':<7} {'Repo':<{name_width}}  Notes")

    libiconv_zip = None
    if platform.os == "windows":
        libiconv_zip = enabled._libiconv_export_zip()

    for idx, repo in enumerate(enabled_repos, 1):
        will_build = repo.name in planned_names
        plan = "On" if will_build else "Off"

        if repo.name == "libiconv" and platform.os == "windows":
            assert libiconv_zip is not None
            found = libiconv_zip.exists()
        else:
            found = enabled._resolve_repo_dir(repo).exists()
        src = "found" if found else "missed"

        notes: list[str] = []
        if state.repo_mode == "selected":
            if repo.name in config.only:
                notes.append("*root")
            elif will_build:
                notes.append("+dep")
        if repo.name in config.skip:
            notes.append("!skip")
        if repo.optional:
            notes.append("opt")
        note_str = " ".join(notes)
        print(f"{idx:>{idx_width}}  {plan:<3}  {src:<7} {repo.name:<{name_width}}  {note_str}")


def _print_plan_summary(config: Config, platform: PlatformInfo, state: UiState, config_path: Path) -> None:
    enabled = _builder_for_enabled_repos(config, platform, state)
    enabled_names = [repo.name for repo in enabled.repos]
    plan_builder = _builder_for_plan(config, platform, state)
    planned_names = {repo.name for repo in (plan_builder.repos if plan_builder else [])}

    print("")
    print("=== Plan ===")
    print(f"Config: {config_path}")
    print(f"Platform: {platform.os} {platform.arch}")
    print(f"Build types: {', '.join(config.build_types)}")
    print(f"Mode: {state.repo_mode}")
    if state.repo_mode == "selected":
        if config.only:
            print(f"Roots (--only): {', '.join(sorted(config.only))}")
        else:
            print("Roots (--only): (none)  [nothing will be built]")
    if config.skip:
        print(f"Skip (--skip): {', '.join(sorted(config.skip))}")
    print(f"dry-run: {'on' if state.dry_run else 'off'}")
    print(f"update: {'on' if not state.no_update else 'off'}")
    print(f"force: {'on' if state.force else 'off'}")
    print(f"force-all: {'on' if state.force_all else 'off'}")
    print(f"jobs: {config.global_cfg.jobs}")

    if platform.os == "windows":
        win_cfg = config.global_cfg.windows
        print(f"windows.generator: {win_cfg.get('generator', 'ninja-msvc')}")
        print(f"windows.msvc_runtime: {win_cfg.get('msvc_runtime', 'static')}")
        print(f"windows.python_wrappers: {win_cfg.get('python_wrappers', 'auto')}")
        print(f"no-ffmpeg: {'on' if state.no_ffmpeg else 'off'}")

    print("")
    print("Repos:")
    width = len(str(len(enabled_names)))
    for idx, name in enumerate(enabled_names, 1):
        will = name in planned_names
        mark = "x" if will else " "
        suffix = ""
        if state.repo_mode == "selected":
            if name in config.only:
                suffix = " *"
            elif will:
                suffix = " +"
        if name in config.skip:
            suffix += " !"
        print(f"{idx:>{width}} [{mark}] {name}{suffix}")

    # Equivalent command line (best-effort).
    parts: list[str] = ["uv", "run", "build.py", "--build-types", ",".join(config.build_types)]
    if state.repo_mode == "selected" and config.only:
        parts += ["--only", ",".join(sorted(config.only))]
    if config.skip:
        parts += ["--skip", ",".join(sorted(config.skip))]
    if state.dry_run:
        parts.append("--dry-run")
    if state.force_all:
        parts.append("--force-all")
    elif state.force:
        parts.append("--force")
    parts.append("--update" if not state.no_update else "--no-update")
    if state.no_ffmpeg:
        parts.append("--no-ffmpeg")
    print("")
    print("Command:")
    print("  " + " ".join(parts))
    print("")


def _menu_select_repos(config: Config, platform: PlatformInfo, state: UiState) -> None:
    while True:
        enabled = _builder_for_enabled_repos(config, platform, state)
        enabled_names = [repo.name for repo in enabled.repos]
        plan_builder = _builder_for_plan(config, platform, state)
        planned_names = {repo.name for repo in (plan_builder.repos if plan_builder else [])}

        print("")
        print("=== Packages ===")
        print(f"Mode: {state.repo_mode}")
        if state.repo_mode == "all":
            print("Selection: [x]=build, [ ]=skipped  (toggle edits --skip)")
        else:
            print("Selection: [x]=build, [ ]=not selected  (*=root, +=dependency)")
        print("Commands: numbers to toggle, 'm' mode, 'a' all, 'n' none, 'q' back")

        width = len(str(len(enabled_names)))
        for idx, name in enumerate(enabled_names, 1):
            will = name in planned_names
            mark = "x" if will else " "
            suffix = ""
            if state.repo_mode == "selected":
                if name in config.only:
                    suffix = " *"
                elif will:
                    suffix = " +"
            if name in config.skip:
                suffix += " !"
            print(f"{idx:>{width}} [{mark}] {name}{suffix}")

        choice = _prompt("packages").lower()
        if not choice:
            continue
        if choice in {"q", "back"}:
            return
        if choice in {"m", "mode"}:
            if state.repo_mode == "all":
                state.repo_mode = "selected"
                config.only = set(state.saved_roots)
            else:
                state.repo_mode = "all"
                state.saved_roots = set(config.only)
                config.only = set()
            continue
        if choice in {"a", "all"}:
            if state.repo_mode == "all":
                config.skip = set()
            else:
                config.only = set(enabled_names)
            continue
        if choice in {"n", "none"}:
            if state.repo_mode == "all":
                config.skip = set(enabled_names)
            else:
                config.only = set()
            continue

        tokens = [t for t in choice.replace(",", " ").split() if t]
        by_index = {str(i): name for i, name in enumerate(enabled_names, 1)}
        raw: list[str] = []
        for tok in tokens:
            raw.append(by_index.get(tok, tok))
        try:
            names = _resolve_repo_names(enabled_names, raw)
        except ValueError as e:
            print(f"error: {e}")
            continue

        if state.repo_mode == "all":
            for name in names:
                if name in config.skip:
                    config.skip.remove(name)
                else:
                    config.skip.add(name)
        else:
            for name in names:
                if name in config.only:
                    config.only.remove(name)
                else:
                    config.only.add(name)


def _menu_build_types(config: Config) -> None:
    value = _prompt("Build types (comma-separated)", ",".join(config.build_types))
    if not value:
        return
    items = [v.strip() for v in value.split(",") if v.strip()]
    build_types = [v.capitalize() if v.lower() != "asan" else "ASAN" for v in items]
    config.build_types = build_types


def _menu_jobs(config: Config) -> None:
    value = _prompt("Jobs (0=auto)", str(config.global_cfg.jobs))
    if not value:
        return
    try:
        config.global_cfg.jobs = int(value)
    except ValueError:
        print("error: jobs must be an integer")


def _menu_paths(config: Config, platform: PlatformInfo, state: UiState) -> None:
    while True:
        builder = Builder(config, platform, dry_run=True, no_update=state.no_update, force=False)
        print("")
        print("=== Paths ===")
        print(f"repo_root: {config.global_cfg.repo_root}")
        print(f"1) src_root: {config.global_cfg.src_root}")
        print(f"2) build_root: {config.global_cfg.build_root}")
        print(f"3) prefix_base: {config.global_cfg.prefix_base or '(default)'}")
        n = 3
        if platform.os == "windows":
            win_cfg = config.global_cfg.windows
            n += 1
            print(f"{n}) windows.install_prefix: {win_cfg.get('install_prefix') or '(unset)'}")
            n_install = n
            n += 1
            print(f"{n}) windows.asan_prefix: {win_cfg.get('asan_prefix') or '(unset)'}")
            n_asan = n
        else:
            n_install = -1
            n_asan = -1

        print("")
        for key in ("Release", "Debug", "ASAN"):
            value = builder.prefixes.get(key)
            if value:
                print(f"install_prefix[{key}]: {value}")
        print("")
        print("Select 1-3 to edit, or 'q' to go back.")

        choice = _prompt("paths").lower()
        if choice in {"q", "back"}:
            return
        if choice == "1":
            v = _prompt("New src_root (relative to repo_root allowed)")
            if v:
                config.global_cfg.src_root = _expand_path(v, config.global_cfg.repo_root)
            continue
        if choice == "2":
            v = _prompt("New build_root (relative to repo_root allowed)")
            if v:
                config.global_cfg.build_root = _expand_path(v, config.global_cfg.repo_root)
            continue
        if choice == "3":
            v = _prompt("New prefix_base (string path; empty = unset)")
            config.global_cfg.prefix_base = v or None
            continue
        if platform.os == "windows" and choice == str(n_install):
            v = _prompt("New windows.install_prefix (empty = unset)")
            if v:
                config.global_cfg.windows["install_prefix"] = v
            else:
                config.global_cfg.windows.pop("install_prefix", None)
            continue
        if platform.os == "windows" and choice == str(n_asan):
            v = _prompt("New windows.asan_prefix (empty = unset)")
            if v:
                config.global_cfg.windows["asan_prefix"] = v
            else:
                config.global_cfg.windows.pop("asan_prefix", None)
            continue


def _menu_toggles(config: Config, platform: PlatformInfo, state: UiState) -> None:
    while True:
        print("")
        print("=== Toggles ===")
        print(f"1) dry-run: {'on' if state.dry_run else 'off'}")
        print(f"2) update: {'on' if not state.no_update else 'off'}")
        print(f"3) force: {'on' if state.force else 'off'}")
        print(f"4) force-all: {'on' if state.force_all else 'off'}")
        idx = 4
        if platform.os == "windows":
            idx += 1
            print(f"{idx}) no-ffmpeg: {'on' if state.no_ffmpeg else 'off'}")
            idx_no_ffmpeg = idx
        else:
            idx_no_ffmpeg = -1
        print("q) back")

        choice = _prompt("toggle").lower()
        if choice in {"q", "back"}:
            return
        if choice == "1":
            state.dry_run = not state.dry_run
            continue
        if choice == "2":
            state.no_update = not state.no_update
            continue
        if choice == "3":
            state.force = not state.force
            if state.force:
                state.force_all = False
            continue
        if choice == "4":
            state.force_all = not state.force_all
            if state.force_all:
                state.force = False
            continue
        if idx_no_ffmpeg != -1 and choice == str(idx_no_ffmpeg):
            state.no_ffmpeg = not state.no_ffmpeg
            if state.no_ffmpeg:
                config.global_cfg.build_ffmpeg = False
                config.global_cfg.windows["build_ffmpeg"] = False
            continue


def _menu_configure(config: Config, platform: PlatformInfo, state: UiState, config_path: Path) -> None:
    while True:
        print("")
        print("=== Configure ===")
        print(f"Mode: {state.repo_mode}")
        print(f"Build types: {', '.join(config.build_types)}")
        if state.repo_mode == "selected":
            roots = ", ".join(sorted(config.only)) if config.only else "(none)"
            print(f"Roots (--only): {roots}")
        if config.skip:
            print(f"Skip (--skip): {', '.join(sorted(config.skip))}")
        print(f"jobs: {config.global_cfg.jobs}")
        print(f"dry-run: {'on' if state.dry_run else 'off'}")
        print(f"update: {'on' if not state.no_update else 'off'}")
        print(f"force: {'on' if state.force else 'off'}")
        print(f"force-all: {'on' if state.force_all else 'off'}")
        if platform.os == "windows":
            print(f"no-ffmpeg: {'on' if state.no_ffmpeg else 'off'}")
        print("")
        print("1) Packages")
        print("2) Build types")
        print("3) Jobs (-j)")
        print("4) Paths")
        print("5) Toggles")
        print("6) Check (plan summary)")
        print("0) Back")
        choice = _prompt("configure").lower()
        if choice in {"0", "q", "back"}:
            return
        if choice == "1":
            _menu_select_repos(config, platform, state)
            continue
        if choice == "2":
            _menu_build_types(config)
            continue
        if choice == "3":
            _menu_jobs(config)
            continue
        if choice == "4":
            _menu_paths(config, platform, state)
            continue
        if choice == "5":
            _menu_toggles(config, platform, state)
            continue
        if choice == "6":
            _print_plan_summary(config, platform, state, config_path)
            _pause()
            continue


def _confirm(prompt: str) -> bool:
    value = _prompt(f"{prompt} (yes/no)", "no")
    b = _parse_bool(value) if value else False
    return bool(b)


def _git_ops(config: Config, platform: PlatformInfo, state: UiState, update: bool, dry_run: bool) -> None:
    plan_builder = _builder_for_plan(config, platform, state)
    if not plan_builder:
        if state.last_plan_error:
            print(f"\nerror: {state.last_plan_error}\n")
            return
        print("\n(no repos selected)\n")
        return
    repos = plan_builder.repos
    if not repos:
        print("\n(no repos planned)\n")
        return
    for repo in repos:
        if repo.name == "libiconv" and platform.os == "windows":
            continue
        if not repo.url:
            continue
        repo_dir = plan_builder._resolve_repo_dir(repo)
        ensure_repo(repo_dir, repo.url, repo.ref, repo.ref_type, update=update, dry_run=dry_run)


def run_console_ui(config: Config, platform: PlatformInfo, config_path: Path) -> int:
    if not sys.stdin.isatty():
        print("error: --tui requires an interactive terminal (TTY).", flush=True)
        return 2

    state = UiState(no_update=bool(config.global_cfg.no_update))
    if platform.os == "windows":
        state.no_ffmpeg = not _ffmpeg_enabled(config, platform)
    if config.only:
        state.repo_mode = "selected"

    while True:
        print("")
        _print_main_packages(config, platform, state)
        print("")
        print("=== Actions ===")
        print("1) Preflight")
        print("2) Configure")
        print("3) Check (plan)")
        print("4) Clone (missing only)")
        print("5) Update (fetch/pull)")
        print("6) Build")
        print("0) Quit")

        choice = _prompt("menu").lower()
        if choice in {"0", "q", "quit", "exit"}:
            return 0
        if choice == "1":
            run_preflight(config, platform, no_update=state.no_update)
            _pause()
            continue
        if choice == "2":
            _menu_configure(config, platform, state, config_path)
            continue
        if choice == "3":
            _print_plan_summary(config, platform, state, config_path)
            _pause()
            continue
        if choice == "4":
            if not _confirm("Clone missing repos?"):
                continue
            _git_ops(config, platform, state, update=False, dry_run=state.dry_run)
            _pause()
            continue
        if choice == "5":
            if not _confirm("Update repos (fetch/pull)?"):
                continue
            _git_ops(config, platform, state, update=True, dry_run=state.dry_run)
            _pause()
            continue
        if choice == "6":
            if state.no_ffmpeg:
                config.global_cfg.build_ffmpeg = False
                config.global_cfg.windows["build_ffmpeg"] = False
            if state.repo_mode == "selected" and not config.only:
                print("\nerror: selected mode requires at least one root repo.\n")
                _pause()
                continue
            try:
                builder = Builder(
                    config,
                    platform,
                    dry_run=state.dry_run,
                    no_update=state.no_update,
                    force=state.force,
                    force_all=state.force_all,
                )
                rc = builder.run()
            except SystemExit as e:
                print(f"\nerror: {e}\n")
                _pause()
                continue
            except Exception as e:
                print(f"\nbuild failed: {e}\n")
                _pause()
                continue
            print(f"\nbuild finished: exit code {rc}\n")
            _pause()
            continue

        print("error: invalid choice")
