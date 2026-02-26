import argparse
import sys
from pathlib import Path

from .config import load_config
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
            "  uv run build.py --build-types Debug,Release\n"
            "  uv run build.py --build-types Debug,ASAN\n"
            "  uv run build.py --build-types Debug,Release --jobs 8\n"
            "  uv run build.py --build-types Debug --only OpenImageIO\n"
            "  uv run build.py --build-types Debug --only OpenImageIO --no-ffmpeg\n"
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

    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)

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

    if args.only:
        config.only = {name.strip() for name in args.only.split(",") if name.strip()}
    if args.skip:
        config.skip = {name.strip() for name in args.skip.split(",") if name.strip()}

    platform_info = detect_platform()
    if args.update:
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
            args.no_update,
            args.dry_run,
        ]
    )
    if not build_requested:
        return run_preflight(config, platform_info, no_update=no_update)

    return builder.run()
