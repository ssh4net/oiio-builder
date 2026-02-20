from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import tomllib


_SCALAR_CACHE_TYPES = (bool, int, float, str)


CacheScalar = bool | int | float | str
CacheValue = CacheScalar | list[CacheScalar]


@dataclass(frozen=True)
class CMakeOptions:
    args: list[str] = field(default_factory=list)
    cache: dict[str, CacheValue] = field(default_factory=dict)

    def merged(self, other: CMakeOptions) -> CMakeOptions:
        merged_args = [*self.args, *other.args]
        merged_cache = {**self.cache, **other.cache}
        return CMakeOptions(args=merged_args, cache=merged_cache)


@dataclass(frozen=True)
class RepoOptions:
    cmake: CMakeOptions = field(default_factory=CMakeOptions)
    platform: dict[str, CMakeOptions] = field(default_factory=dict)

    def resolve(self, platform_os: str) -> CMakeOptions:
        options = self.cmake
        platform_opts = self.platform.get(platform_os)
        if platform_opts is None:
            return options
        return options.merged(platform_opts)


def _coerce_cache_value(value: Any, context: str) -> CacheValue:
    if value is None:
        raise TypeError(f"{context}: cache value cannot be null")
    if isinstance(value, _SCALAR_CACHE_TYPES):
        return value
    if isinstance(value, list):
        coerced: list[CacheScalar] = []
        for idx, item in enumerate(value):
            if not isinstance(item, _SCALAR_CACHE_TYPES):
                raise TypeError(f"{context}: cache list item {idx} has unsupported type: {type(item).__name__}")
            coerced.append(item)
        return coerced
    raise TypeError(f"{context}: cache value has unsupported type: {type(value).__name__}")


def _parse_cmake_options(table: Any, context: str) -> CMakeOptions:
    if not table:
        return CMakeOptions()
    if not isinstance(table, dict):
        raise TypeError(f"{context}: expected table, got {type(table).__name__}")

    raw_args = table.get("args", [])
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list) or any(not isinstance(item, str) for item in raw_args):
        raise TypeError(f"{context}.args: expected list[str]")
    args = [item.strip() for item in raw_args if item.strip()]

    cache: dict[str, CacheValue] = {}
    raw_cache = table.get("cache", {})
    if raw_cache is None:
        raw_cache = {}
    if not isinstance(raw_cache, dict):
        raise TypeError(f"{context}.cache: expected table")
    for key, raw_value in raw_cache.items():
        if not isinstance(key, str) or not key.strip():
            raise TypeError(f"{context}.cache: keys must be non-empty strings")
        cache[str(key)] = _coerce_cache_value(raw_value, f"{context}.cache.{key}")

    return CMakeOptions(args=args, cache=cache)


def _parse_repo_options(data: dict[str, Any], context: str) -> RepoOptions:
    cmake = _parse_cmake_options(data.get("cmake", {}), f"{context}.cmake")

    platform: dict[str, CMakeOptions] = {}
    raw_platform = data.get("platform", {})
    if raw_platform is None:
        raw_platform = {}
    if raw_platform:
        if not isinstance(raw_platform, dict):
            raise TypeError(f"{context}.platform: expected table")
        for os_name, os_table in raw_platform.items():
            if not isinstance(os_name, str) or not os_name.strip():
                raise TypeError(f"{context}.platform: keys must be non-empty strings")
            if not isinstance(os_table, dict):
                raise TypeError(f"{context}.platform.{os_name}: expected table")
            platform[str(os_name)] = _parse_cmake_options(os_table.get("cmake", {}), f"{context}.platform.{os_name}.cmake")

    return RepoOptions(cmake=cmake, platform=platform)


def load_repo_defaults(defaults_dir: Path) -> dict[str, RepoOptions]:
    """Load fixed per-repo defaults from *.toml files under defaults_dir."""
    if not defaults_dir.is_dir():
        return {}

    options: dict[str, RepoOptions] = {}
    for path in sorted(defaults_dir.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        repo_names = data.get("repo_names")
        if repo_names is None:
            names = [path.stem]
        else:
            if not isinstance(repo_names, list) or any(not isinstance(item, str) for item in repo_names):
                raise TypeError(f"{path}: repo_names must be a list[str]")
            names = [item.strip() for item in repo_names if item.strip()]
            if not names:
                raise TypeError(f"{path}: repo_names must not be empty")
        repo_opts = _parse_repo_options(data, str(path))
        for name in names:
            if name in options:
                raise ValueError(f"Duplicate repo options for {name} (from {path})")
            options[name] = repo_opts
    return options


def load_user_overrides(path: Path) -> dict[str, RepoOptions]:
    """Load local overrides from build.user.toml (gitignored)."""
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    entries = data.get("repo_overrides", [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise TypeError(f"{path}: repo_overrides must be an array of tables")

    options: dict[str, RepoOptions] = {}
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TypeError(f"{path}: repo_overrides[{idx}] must be a table")
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise TypeError(f"{path}: repo_overrides[{idx}].name must be a non-empty string")
        name = name.strip()
        if name in options:
            raise ValueError(f"{path}: duplicate repo_overrides entry for {name}")
        options[name] = _parse_repo_options(entry, f"{path}:repo_overrides[{idx}]")
    return options


def _format_cache_value(value: CacheValue) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, (int, float, str)):
        return str(value)
    parts: list[str] = []
    for item in value:
        if isinstance(item, bool):
            parts.append("ON" if item else "OFF")
        else:
            parts.append(str(item))
    return ";".join(parts)


def render_cmake_options(options: CMakeOptions) -> list[str]:
    args = list(options.args)
    for key in sorted(options.cache.keys()):
        args.append(f"-D{key}={_format_cache_value(options.cache[key])}")
    return args

