import argparse
import os
from pathlib import Path

from .config import load_config
from .core import Builder
from .platform import detect_platform


def _parse_build_types(value: str) -> list[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    return [v.capitalize() if v.lower() != "asan" else "ASAN" for v in items]


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-platform build orchestrator")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[1] / "build.toml"),
        help="Path to build.toml",
    )
    parser.add_argument("--build-types", help="Comma-separated: Debug,Release,ASAN")
    parser.add_argument("--only", help="Comma-separated repo names")
    parser.add_argument("--skip", help="Comma-separated repo names")
    parser.add_argument("--no-update", action="store_true", help="Skip git fetch/pull (overrides config)")
    parser.add_argument("--update", action="store_true", help="Force git fetch/pull (overrides config)")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only")
    parser.add_argument("--force", action="store_true", help="Force rebuild, ignore stamps")
    parser.add_argument("--list-repos", action="store_true", help="List configured repos")
    parser.add_argument("--print-prefixes", action="store_true", help="Print install prefixes and exit")

    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)

    if args.build_types:
        config.build_types = _parse_build_types(args.build_types)

    if args.only:
        config.only = {name.strip() for name in args.only.split(",") if name.strip()}
    if args.skip:
        config.skip = {name.strip() for name in args.skip.split(",") if name.strip()}

    platform_info = detect_platform()
    if args.update:
        no_update = False
    else:
        no_update = args.no_update or config.global_cfg.no_update
    builder = Builder(config, platform_info, dry_run=args.dry_run, no_update=no_update, force=args.force)

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

    return builder.run()
