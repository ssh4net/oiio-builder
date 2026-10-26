from __future__ import annotations

import configparser
from pathlib import Path

from ..runner import run


def patch_cmake_block(path: Path, label: str, block: str, *, before: str | None = None) -> None:
    if not path.is_file():
        raise RuntimeError(f"Geometry recipe CMake patch target is missing: {path}")
    with path.open(encoding="utf-8", newline="") as source:
        original = source.read()
    newline = "\r\n" if "\r\n" in original else "\n"
    text = original.replace("\r\n", "\n")
    begin = f"# OIIO_BUILDER_{label}_BEGIN"
    end = f"# OIIO_BUILDER_{label}_END"
    replacement = f"{begin}\n{block.rstrip()}\n{end}"
    if begin in text:
        start = text.index(begin)
        if end not in text[start:]:
            raise RuntimeError(f"Incomplete geometry recipe patch {label}: {path}")
        stop = text.index(end, start) + len(end)
        updated = text[:start] + replacement + text[stop:]
    elif before is not None:
        if before not in text:
            raise RuntimeError(f"Geometry recipe patch {label} no longer matches upstream source: {path}")
        updated = text.replace(before, replacement + "\n\n" + before, 1)
    else:
        updated = text.rstrip() + "\n\n" + replacement + "\n"
    if updated != text:
        with path.open("w", encoding="utf-8", newline="") as source:
            source.write(updated.replace("\n", newline))


def initialize_missing_submodules(builder, ctx, env: dict[str, str]) -> None:
    manifest = ctx.src_dir / ".gitmodules"
    if not manifest.is_file():
        return
    modules = configparser.ConfigParser()
    modules.read(manifest, encoding="utf-8")
    missing = []
    for section in modules.sections():
        relative = modules.get(section, "path")
        directory = ctx.src_dir / relative
        if not directory.is_dir() or not any(item.name != ".git" for item in directory.iterdir()):
            missing.append(relative)
    if missing:
        # Only initialize absent checkouts; never reset populated user submodules.
        run(["git", "-C", str(ctx.src_dir), "submodule", "update", "--init", "--recursive", "--", *missing],
            env=env, dry_run=builder.dry_run,
            log_path=str(builder._repo_log_path(ctx.repo.name, ctx.build_type, "submodules")))
