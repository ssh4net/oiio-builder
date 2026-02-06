# Repository Guidelines

This repository contains a cross-platform Python build orchestrator for building OpenImageIO and its dependency stack into reproducible install prefixes (mirrors the legacy shell stack script).

## Project Structure & Module Organization

- `build.py`: CLI entry point.
- `build.toml`: build policy + `[[repos]]` dependency graph (URLs, refs, build systems, toggles).
- `builder/`: implementation (`cli.py`, `config.py`, `core.py`, `git_ops.py`, `preflight.py`, `stamps.py`, …).
- `build_MOS_stack_until_OIIO.sh`: legacy reference for ordering/options.
- `verify_static_prefix.sh`: sanity checks for a produced prefix (static-only, PIC, etc.).
- Docs: `README.md`, `builder_design.md`, `REPOS.md`.

By default, sources/build/install live *outside* the repo (see `[global].src_root`, `[global].build_root`, and `[global].prefix_base` in `build.toml`).

## Build, Test, and Development Commands

```bash
uv venv
uv run build.py            # no args = preflight report and exit
uv run build.py --list-repos
uv run build.py --build-types Debug,Release --only zlib-ng,libpng
uv run build.py --dry-run --only OpenImageIO
uv run build.py --force    # ignore stamps and rebuild
./verify_static_prefix.sh ../_install/UBS
```

Notes:
- Stamps live under `../_build_py/.stamps` by default; use `--force` when iterating on build logic.
- Git updates are controlled by `no_update` in `build.toml` and `--update/--no-update`.

## Coding Style & Naming Conventions

- Python 3.11+ (uses `tomllib`), 4-space indentation, type hints, and `pathlib.Path`.
- Keep modules small and single-purpose; prefer explicit inputs/outputs over global state.
- Naming: `snake_case` for functions/files, `CapWords` for classes, `UPPER_SNAKE_CASE` for constants.

## Testing Guidelines

There is no dedicated unit test suite yet. Validate changes with:
- `uv run build.py --preflight` (tools + repo visibility + computed prefixes).
- `--dry-run` for command review and `--only ...` for fast “small stack” builds.
- `./verify_static_prefix.sh <prefix>` when changing static/prefix/linkage behavior.

## Commit & Pull Request Guidelines

- Commit subjects in this repo are short, imperative, and capitalized (e.g., “Add …”, “Fix …”), sometimes with extra detail after `:`/`;`.
- PRs should include: platforms tested, exact command(s) used, and any `build.toml` changes explained (new repo URL/ref, toggles, or tool overrides like `[windows.env]`).
- If you add/change CLI flags or config keys, update `README.md` accordingly.

