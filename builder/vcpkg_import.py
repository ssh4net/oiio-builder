from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import shutil
import zipfile

from .runner import banner


def resolve_export_zip(
    builder: Any,
    env: dict[str, str] | None,
    *,
    default_filename: str,
    env_names: tuple[str, ...],
    glob_pattern: str,
) -> Path:
    cfg = builder.config.global_cfg
    default = cfg.repo_root / "external" / default_filename
    override = None
    if env:
        for name in env_names:
            override = env.get(name)
            if override:
                break
    if not override and builder.platform.os == "windows":
        for source in (cfg.windows_env, cfg.env, os.environ):
            for name in env_names:
                override = source.get(name)
                if override:
                    break
            if override:
                break
    if override:
        path = Path(os.path.expandvars(override)).expanduser()
        if not path.is_absolute():
            path = (cfg.repo_root / path).resolve()
        return path

    external_dir = cfg.repo_root / "external"
    if default.exists():
        return default
    if external_dir.is_dir():
        matches = sorted(external_dir.glob(glob_pattern))
        if matches:
            return matches[0]
    return default


def stage_export(builder: Any, ctx: Any, env: dict[str, str], zip_path: Path, stage_name: str) -> Path | None:
    ctx.build_dir.mkdir(parents=True, exist_ok=True)
    if not zip_path.exists():
        raise RuntimeError(f"Missing {ctx.repo.name} vcpkg export zip: {zip_path}")

    banner(f"{ctx.repo.name} ({ctx.build_type}) - stage")
    print(f"vcpkg export zip: {zip_path}", flush=True)

    export_dir = ctx.build_dir / stage_name
    marker = export_dir / ".zipstamp"
    st = zip_path.stat()
    stamp = f"{zip_path}|{int(st.st_size)}|{int(st.st_mtime)}"

    if builder.dry_run:
        print(f"[dry-run] extract -> {export_dir}", flush=True)
        return None

    if marker.exists() and marker.read_text(encoding="utf-8").strip() == stamp:
        return _find_export_root(export_dir) / "installed"

    if export_dir.exists():
        shutil.rmtree(export_dir, ignore_errors=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        export_abs = export_dir.resolve()
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            dest = export_dir / name
            dest_abs = dest.resolve()
            if export_abs not in dest_abs.parents and dest_abs != export_abs:
                raise RuntimeError(f"Refusing to extract outside destination: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src_f, open(dest, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
    marker.write_text(stamp, encoding="utf-8")
    return _find_export_root(export_dir) / "installed"


def find_triplet(installed_dir: Path, marker_rel: str, zip_path: Path, *, prefer_static: bool = True) -> Path:
    marker_parts = Path(marker_rel).parts
    triplet_candidates = [
        p
        for p in installed_dir.iterdir()
        if p.is_dir() and p.name != "vcpkg" and (p.joinpath(*marker_parts)).exists()
    ]
    if not triplet_candidates:
        raise RuntimeError(f"vcpkg export zip does not contain installed/<triplet>/{marker_rel}: {zip_path}")

    def _triplet_score(path: Path) -> tuple[int, str]:
        name = path.name.lower()
        score = 0
        is_static = "static" in name
        if is_static == prefer_static:
            score += 10
        bin_dir = path / "bin"
        if not prefer_static and bin_dir.is_dir() and any(bin_dir.glob("*.dll")):
            score += 5
        return score, name

    triplet_candidates.sort(key=_triplet_score, reverse=True)
    return triplet_candidates[0]


def add_debug_postfix(filename: str, debug_postfix: str) -> str:
    p = Path(filename)
    suffixes = p.suffixes
    if not suffixes:
        return filename + debug_postfix
    base = filename
    for suff in suffixes:
        if base.endswith(suff):
            base = base[: -len(suff)]
    if base.endswith(debug_postfix):
        return filename
    return base + debug_postfix + "".join(suffixes)


def copy_include_tree(include_src: Path, include_dst: Path) -> None:
    include_dst.mkdir(parents=True, exist_ok=True)
    for item in include_src.iterdir():
        dest = include_dst / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        elif item.is_file():
            shutil.copy2(item, dest)


def copy_bin_payload(bin_src: Path, bin_dst: Path, package: str, *, prefer_static: bool = True) -> None:
    if not bin_src.is_dir():
        return
    bin_dst.mkdir(parents=True, exist_ok=True)
    if prefer_static and any(bin_src.glob("*.dll")):
        print(f"[note] {package} export contains DLLs; prefer exporting a *-static triplet for a fully static prefix", flush=True)
    for item in bin_src.iterdir():
        if item.is_file() and item.suffix.lower() in {".dll", ".pdb", ".exe"}:
            shutil.copy2(item, bin_dst / item.name)


def _find_export_root(base: Path) -> Path:
    if (base / "installed").is_dir():
        return base
    for child in base.iterdir():
        if child.is_dir() and (child / "installed").is_dir():
            return child
    raise RuntimeError(f"Unexpected vcpkg export layout under {base}")
