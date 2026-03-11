from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import stat
import sys
import time

from ..git_ops import git_head
from ..runner import banner, print_cmd
from .policy import imageio_enabled

STAMP_REVISION = "3"


def enabled(builder, _repo) -> bool:
    cfg = builder.config.global_cfg
    only = getattr(builder.config, "only", set()) or set()
    explicitly_selected = any(str(name).strip().lower() == "glslang" for name in only)
    return explicitly_selected or (imageio_enabled(builder) and bool(cfg.build_oiio))


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
        trimmed = trimmed[1:-1]
    return trimmed or None


def _python_executable(builder) -> str:
    cfg = builder.config.global_cfg
    env = dict(os.environ)
    env.update(cfg.env)
    if builder.platform.os == "windows":
        env.update(cfg.windows_env)

    candidates = [
        _normalize_override(
            env.get("Python3_EXECUTABLE")
            or env.get("PYTHON3_EXECUTABLE")
            or env.get("Python_EXECUTABLE")
            or env.get("PYTHON_EXECUTABLE")
        ),
        sys.executable,
        "python3",
        "python",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() and path.exists():
            return str(path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return sys.executable


def cmake_args(builder, _ctx) -> list[str]:
    python_exec = Path(_python_executable(builder)).as_posix()
    return [
        f"-DPython3_EXECUTABLE={python_exec}",
        f"-DPython_EXECUTABLE={python_exec}",
    ]


def _source_stamp_path(builder) -> Path:
    return builder.config.global_cfg.build_root / ".source-prep" / "glslang.json"


def _bool_from_cache_value(value: object, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "on", "true", "yes", "y"}:
            return True
        if lowered in {"0", "off", "false", "no", "n", ""}:
            return False
    return default


def _enable_opt(builder) -> bool:
    options = builder._repo_cmake_effective_toml_options("glslang")
    return _bool_from_cache_value(options.cache.get("ENABLE_OPT"), default=True)


def _read_source_stamp(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(key, str) and isinstance(value, str)}


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return

    def _onerror(func, failed_path, _exc_info) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        func(failed_path)

    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path, onerror=_onerror)
        except FileNotFoundError:
            return
        except OSError as exc:
            last_error = exc

        if not path.exists() and not path.is_symlink():
            return
        time.sleep(0.1)

    if path.exists() or path.is_symlink():
        raise RuntimeError(f"glslang: failed to remove existing path {path}") from last_error


def _copy_repo_tree(src: Path, dst: Path) -> None:
    _remove_path(dst)
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(".git", ".github", ".gitlab"),
    )


def patch_source(builder, src_dir: Path) -> None:
    if not _enable_opt(builder):
        return

    repo_head = git_head(src_dir)
    spirv_tools_src = builder.repo_paths.get("SPIRV-Tools")
    spirv_headers_src = builder.repo_paths.get("SPIRV-Headers")
    if spirv_tools_src is None or spirv_headers_src is None:
        raise RuntimeError("glslang: SPIRV-Tools/SPIRV-Headers repo paths are unavailable")

    if not spirv_tools_src.exists():
        if builder.dry_run:
            print(f"[dry-run] glslang: expected SPIRV-Tools checkout at {spirv_tools_src}", flush=True)
            return
        raise RuntimeError(f"glslang: missing SPIRV-Tools checkout at {spirv_tools_src}")
    if not spirv_headers_src.exists():
        if builder.dry_run:
            print(f"[dry-run] glslang: expected SPIRV-Headers checkout at {spirv_headers_src}", flush=True)
            return
        raise RuntimeError(f"glslang: missing SPIRV-Headers checkout at {spirv_headers_src}")

    spirv_tools_head = git_head(spirv_tools_src)
    spirv_headers_head = git_head(spirv_headers_src)
    stamp_path = _source_stamp_path(builder)
    stamp = _read_source_stamp(stamp_path)
    expected = {
        "enable_opt": "1",
        "glslang_head": repo_head or "",
        "spirv_tools_head": spirv_tools_head or "",
        "spirv_headers_head": spirv_headers_head or "",
    }
    if all(stamp.get(key, "") == value for key, value in expected.items()):
        return

    spirv_tools_dst = src_dir / "External" / "spirv-tools"
    spirv_headers_dst = spirv_tools_dst / "external" / "spirv-headers"
    print_cmd("stage sources", [str(spirv_tools_src), "->", str(spirv_tools_dst)])
    print_cmd("stage sources", [str(spirv_headers_src), "->", str(spirv_headers_dst)])
    banner("glslang - stage external sources")

    if builder.dry_run:
        print(f"[dry-run] stage {spirv_tools_src} -> {spirv_tools_dst}", flush=True)
        print(f"[dry-run] stage {spirv_headers_src} -> {spirv_headers_dst}", flush=True)
        return

    spirv_tools_dst.parent.mkdir(parents=True, exist_ok=True)
    _copy_repo_tree(spirv_tools_src, spirv_tools_dst)
    spirv_headers_dst.parent.mkdir(parents=True, exist_ok=True)
    _copy_repo_tree(spirv_headers_src, spirv_headers_dst)

    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8")
