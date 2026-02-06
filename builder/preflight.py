from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import os

from .config import Config
from .core import Builder
from .platform import PlatformInfo

_PREFLIGHT_REPO_URLS: dict[str, str] = {
    "libxml2": "https://gitlab.gnome.org/GNOME/libxml2.git",
    "pugixml": "https://github.com/zeux/pugixml.git",
    "expat": "https://github.com/libexpat/libexpat.git",
    "yaml-cpp": "https://github.com/jbeder/yaml-cpp.git",
    "pybind11": "https://github.com/pybind/pybind11.git",
    "lcms2": "https://github.com/mm2/Little-CMS.git",
    "glew": "https://github.com/Perlmint/glew-cmake.git",
    "glfw": "https://github.com/glfw/glfw.git",
    "pystring": "https://github.com/imageworks/pystring.git",
}


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


def _normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
        trimmed = trimmed[1:-1]
    return trimmed or None


def _tool_checks(platform: PlatformInfo, env: dict[str, str]) -> list[ToolCheck]:
    doxygen_override = _normalize_override(env.get("DOXYGEN_EXECUTABLE"))
    pkg_override = _normalize_override(env.get("PKG_CONFIG_EXECUTABLE") or env.get("PKG_CONFIG"))
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
    env = dict(os.environ)
    env.update(config.global_cfg.env)
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
        url = repo.url or _PREFLIGHT_REPO_URLS.get(repo.name, "")
        show_url = repo.name in _PREFLIGHT_REPO_URLS and bool(url)
        url_suffix = f", url={url}" if show_url else ""
        if repo.name == "libiconv" and platform.os == "windows":
            zip_path = builder._libiconv_export_zip()
            if zip_path.exists():
                lines.append(f"  {repo.name}: ok (vcpkg export zip: {zip_path})")
            else:
                lines.append(f"  {repo.name}: missing (vcpkg export zip expected at {zip_path})")
                if not repo.optional:
                    missing_repos += 1
            continue
        path = builder._resolve_repo_dir(repo)
        if path.exists():
            lines.append(f"  {repo.name}: ok ({path}{url_suffix})")
            continue
        if repo.optional and not repo.url:
            lines.append(f"  {repo.name}: missing (optional{url_suffix})")
        else:
            missing_repos += 1
            lines.append(f"  {repo.name}: missing (expected at {path}, url={url or 'no-url'})")

    lines.append("Summary:")
    lines.append(f"  missing tools: {missing_tools}")
    lines.append(f"  missing repos: {missing_repos}")
    lines.append("Example build:")
    lines.append("  uv run build.py --build-types Debug,Release")
    print("\n".join(lines))
    return 0
