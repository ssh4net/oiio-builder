from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from .config import Config
from .core import Builder
from .platform import PlatformInfo


@dataclass
class ToolCheck:
    name: str
    candidates: list[str]
    required: bool
    note: str = ""


def _find_any(candidates: list[str]) -> tuple[str | None, str]:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path, name
    return None, ""


def _tool_checks(platform: PlatformInfo, env: dict[str, str]) -> list[ToolCheck]:
    doxygen_override = env.get("DOXYGEN_EXECUTABLE")
    pkg_override = env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG")
    checks = [
        ToolCheck("git", ["git"], True),
        ToolCheck("cmake", ["cmake"], True),
        ToolCheck("ninja", ["ninja"], True),
        ToolCheck("pkg-config", [pkg_override] if pkg_override else ["pkg-config", "pkgconf"], True),
        ToolCheck("doxygen", [doxygen_override] if doxygen_override else ["doxygen"], True),
        ToolCheck("sphinx-build", ["sphinx-build", "sphinx-build-3"], True),
    ]
    if platform.os in {"macos", "linux"}:
        checks.append(ToolCheck("make", ["make", "gmake"], True))
    if platform.arch == "x86_64":
        checks.append(ToolCheck("nasm", ["nasm", "yasm"], True, "x86/x64 asm"))
    else:
        checks.append(ToolCheck("nasm", ["nasm", "yasm"], False, "not required on arm64"))
    checks.append(ToolCheck("python", ["python3", "python"], True))
    checks.append(ToolCheck("uv", ["uv"], False, "optional"))
    if platform.os == "macos":
        checks.append(ToolCheck("xcrun", ["xcrun"], True))
    return checks


def run_preflight(config: Config, platform: PlatformInfo, no_update: bool) -> int:
    builder = Builder(config, platform, dry_run=True, no_update=no_update, force=False)
    env = dict(config.global_cfg.env)
    if platform.os == "windows":
        env.update(config.global_cfg.windows_env)
    missing_tools = 0
    missing_repos = 0

    lines = ["", "=== Preflight Report ==="]
    lines.append(f"Platform: {platform.os} {platform.arch}")
    lines.append("Paths:")
    lines.append(f"  repo_root: {config.global_cfg.repo_root}")
    lines.append(f"  src_root: {config.global_cfg.src_root}")
    lines.append(f"  build_root: {config.global_cfg.build_root}")
    lines.append(f"  prefix_base: {config.global_cfg.prefix_base or '(default)'}")
    for key in ("Release", "Debug", "ASAN"):
        prefix = builder.prefixes.get(key)
        if prefix:
            lines.append(f"  install_prefix[{key}]: {prefix}")

    if builder.toolchain:
        lines.append("Toolchain:")
        for key in ("cc", "cxx", "ld", "ar", "ranlib"):
            value = builder.toolchain.get(key)
            if value:
                lines.append(f"  {key}: {value}")
    lines.append("Tools:")
    for check in _tool_checks(platform, env):
        path, resolved = _find_any(check.candidates)
        if path:
            note = f" ({resolved})" if resolved and resolved != check.name else ""
            lines.append(f"  {check.name}: ok ({path}){note}")
            continue
        if check.required:
            missing_tools += 1
            extra = f" [{check.note}]" if check.note else ""
            lines.append(f"  {check.name}: missing{extra}")
        else:
            extra = f" [{check.note}]" if check.note else ""
            lines.append(f"  {check.name}: not found{extra}")

    lines.append("Repos:")
    for repo in builder.repos:
        path = builder._resolve_repo_dir(repo)
        if path.exists():
            lines.append(f"  {repo.name}: ok ({path})")
            continue
        if repo.optional and not repo.url:
            lines.append(f"  {repo.name}: missing (optional)")
        else:
            missing_repos += 1
            url = repo.url or "no-url"
            lines.append(f"  {repo.name}: missing (expected at {path}, url={url})")

    lines.append("Summary:")
    lines.append(f"  missing tools: {missing_tools}")
    lines.append(f"  missing repos: {missing_repos}")
    print("\n".join(lines))
    return 0
