import argparse
import subprocess
import sys
from pathlib import Path

from .config import Config, RepoConfig, load_config
from .git_ops import force_update_repo, resolve_repo_dir
from .core import Builder
from .platform import detect_platform
from .preflight import run_preflight


def _parse_build_types(value: str) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    return [v.capitalize() if v.lower() != "asan" else "ASAN" for v in items]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cross-platform build orchestrator",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  uv run build.py --preflight\n"
            "  uv run build.py --update-only\n"
            "  uv run build.py --force-update\n"
            "  uv run build.py --prepare-only --only Qt6\n"
            "  uv run build.py --build-types Debug,Release\n"
            "  uv run build.py --build-types Debug,ASAN\n"
            "  uv run build.py --build-types Debug,Release --jobs 8\n"
            "  uv run build.py --build-types Debug --only OpenImageIO\n"
            "  uv run build.py --build-types Debug --only OpenImageIO --no-ffmpeg\n"
            "  uv run build.py --profile nongpl-static --build-types Release\n"
            "  uv run build.py --profile lgpl-dynamic --build-types Debug,Release\n"
            "  uv run build.py --build-types Debug --force\n"
            "  uv run build.py --build-types Debug --force-all\n"
            "  uv run build.py --skip libheif,libwebp\n"
        ),
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "build.toml"),
        help="Path to build.toml",
    )
    parser.add_argument("--build-types", help="Comma-separated: Debug,Release,ASAN")
    parser.add_argument(
        "--jobs",
        type=int,
        default=None,
        help="Parallel build jobs. 0 means auto (overrides config)",
    )
    parser.add_argument("--only", help="Comma-separated repo names")
    parser.add_argument("--skip", help="Comma-separated repo names")
    parser.add_argument("--no-update", action="store_true", help="Skip git fetch/pull (overrides config)")
    parser.add_argument("--update", action="store_true", help="Force git fetch/pull (overrides config)")
    parser.add_argument("--update-only", action="store_true", help="Clone/fetch/checkout repos only, then exit")
    parser.add_argument(
        "--force-update",
        action="store_true",
        help="Interactively discard local source changes and refresh existing Git checkouts; must be used alone",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Clone/fetch/checkout repos, run source-prep hooks (e.g. Qt init-repository), then exit",
    )
    parser.add_argument(
        "--apply-prefix-contract",
        action="store_true",
        help="With --prepare-only, write/update managed CMakeUserPresets.json shims that point at the active prefix contract",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands only")
    parser.add_argument(
        "--no-ffmpeg",
        action="store_true",
        help="Disable FFmpeg (also disables OpenImageIO ffmpeg plugin detection)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Force rebuild selected repos. "
            "With --only, forces only explicitly listed repos; "
            "without --only, same as --force-all."
        ),
    )
    parser.add_argument("--force-all", action="store_true", help="Force rebuild all repos in this run, ignore stamps")
    parser.add_argument(
        "--reinstall",
        action="store_true",
        help=(
            "Force reinstall selected repos (install step only when up-to-date). "
            "With --only, reinstalls only explicitly listed repos; "
            "without --only, same as --reinstall-all."
        ),
    )
    parser.add_argument("--reinstall-all", action="store_true", help="Force reinstall all repos in this run")
    parser.add_argument(
        "--parallel-build-types",
        action="store_true",
        help="Build multiple configs in parallel (macOS/Linux only). Splits --jobs across build types.",
    )
    parser.add_argument("--no-ccache", action="store_true", help="Disable ccache compiler launcher (if installed)")
    parser.add_argument("--preflight", action="store_true", help="Run tool/repo checks and exit")
    parser.add_argument("--list-repos", action="store_true", help="List configured repos")
    parser.add_argument("--print-prefixes", action="store_true", help="Print install prefixes and exit")
    parser.add_argument(
        "--profile",
        help="License/linkage profile. Supported: nongpl-static, lgpl-dynamic",
    )

    args = parser.parse_args()
    if args.force_update:
        extra_args = [arg for arg in sys.argv[1:] if arg != "--force-update"]
        if extra_args:
            parser.error("--force-update must be used alone; it cannot be combined with other options")

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path, profile_override=args.profile)

    if args.force_update:
        return _run_force_update(config)

    if args.build_types:
        config.build_types = _parse_build_types(args.build_types)

    if args.jobs is not None:
        if args.jobs < 0:
            raise SystemExit("--jobs must be >= 0")
        config.global_cfg.jobs = args.jobs
    if args.no_ccache:
        config.global_cfg.use_ccache = False

    if args.no_ffmpeg:
        config.global_cfg.build_ffmpeg = False
        config.global_cfg.windows["build_ffmpeg"] = False
        config.global_cfg.windows["use_ffmpeg_from_prefix"] = False

    if args.only:
        config.only = {name.strip() for name in args.only.split(",") if name.strip()}
    if args.skip:
        config.skip = {name.strip() for name in args.skip.split(",") if name.strip()}

    platform_info = detect_platform()
    if args.update_only and args.prepare_only:
        raise SystemExit("--update-only cannot be combined with --prepare-only")
    if args.apply_prefix_contract and not args.prepare_only:
        raise SystemExit("--apply-prefix-contract requires --prepare-only")
    if args.update_only and args.no_update:
        raise SystemExit("--update-only cannot be combined with --no-update")
    if args.update_only or args.update:
        no_update = False
    else:
        no_update = args.no_update or config.global_cfg.no_update

    if args.preflight or len(sys.argv) == 1:
        return run_preflight(config, platform_info, no_update=no_update)

    builder = Builder(
        config,
        platform_info,
        dry_run=args.dry_run,
        no_update=no_update,
        force=args.force,
        force_all=args.force_all,
        reinstall=args.reinstall,
        reinstall_all=args.reinstall_all,
        parallel_build_types=args.parallel_build_types,
        apply_prefix_contract=args.apply_prefix_contract,
    )

    if args.list_repos:
        for repo in builder.repos:
            status = "enabled" if repo.enabled else "disabled"
            print(f"{repo.name} ({status})")
        return 0

    if args.print_prefixes:
        prefixes = builder.prefixes
        for key in ("Release", "Debug", "ASAN"):
            value = prefixes.get(key)
            if value:
                print(f"{key}: {value}")
        return 0

    if args.update_only:
        return builder.update_only()
    if args.prepare_only:
        return builder.prepare_only()

    build_requested = any(
        [
            args.build_types,
            args.only,
            args.skip,
            args.no_ffmpeg,
            args.force,
            args.force_all,
            args.reinstall,
            args.reinstall_all,
            args.update,
            args.update_only,
            args.prepare_only,
            args.no_update,
            args.dry_run,
            args.profile,
        ]
    )
    if not build_requested:
        return run_preflight(config, platform_info, no_update=no_update)

    return builder.run()


def _run_force_update(config: Config) -> int:
    """Confirm and force-refresh every existing configured source checkout."""
    targets: list[tuple[RepoConfig, Path]] = []
    skipped: list[str] = []
    for repo in config.repos:
        if not repo.enabled:
            continue
        path = resolve_repo_dir(config.global_cfg.src_root, repo.dir, repo.dir_candidates)
        if not path.exists():
            skipped.append(f"{repo.name} (missing)")
            continue
        if not (path / ".git").exists():
            skipped.append(f"{repo.name} (not a Git checkout: {path})")
            continue
        targets.append((repo, path))

    print("WARNING: --force-update is destructive.")
    print(f"It will restore {len(targets)} existing checkout(s) under {config.global_cfg.src_root} to their configured upstream revisions.")
    print("For every listed checkout it discards tracked changes and local commits, then removes untracked non-ignored files.")
    print("It does not clone missing sources, remove ignored files, or run any build step.")
    if targets:
        print("Checkouts:")
        for repo, path in targets:
            print(f"  {repo.name}: {path}")
    if skipped:
        print(f"Skipped: {', '.join(skipped)}")

    try:
        confirmation = input("Type FORCE-UPDATE to continue: ").strip()
    except EOFError:
        confirmation = ""
    if confirmation != "FORCE-UPDATE":
        print("Force update cancelled; no sources were changed.")
        return 1

    updated = 0
    incomplete: list[str] = []
    for repo, path in targets:
        try:
            if force_update_repo(path, repo.url, repo.ref, repo.ref_type):
                updated += 1
            else:
                incomplete.append(repo.name)
        except subprocess.CalledProcessError as exc:
            incomplete.append(repo.name)
            print(f"[warning] {repo.name}: source refresh failed ({exc}); continuing with other checkouts", flush=True)

    if incomplete:
        print(f"Force update incomplete: {updated}/{len(targets)} checkout(s) refreshed; review: {', '.join(incomplete)}.")
        return 1
    print(f"Force update completed: {updated}/{len(targets)} checkout(s) refreshed.")
    return 0
