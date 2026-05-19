from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import os
import shutil


def normalize_override(value: str | None) -> str | None:
    if not value:
        return None
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"\"", "'"}:
        trimmed = trimmed[1:-1]
    return trimmed or None


def resolve_executable_candidate(candidate: str | None, *, search_path: str | None = None) -> str | None:
    normalized = normalize_override(candidate)
    if not normalized:
        return None

    if any(sep in normalized for sep in ("/", "\\")):
        expanded = os.path.expandvars(os.path.expanduser(normalized))
        path = Path(expanded)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None

    return shutil.which(normalized, path=search_path)


def append_unique_candidate(candidates: list[str], seen: set[str], value: str | None) -> None:
    normalized = normalize_override(value)
    if not normalized:
        return
    key = os.path.normcase(os.path.normpath(normalized.strip("\"'")))
    if key in seen:
        return
    seen.add(key)
    candidates.append(normalized)


def windows_nasm_probe_candidates(env: Mapping[str, str] | None = None) -> list[str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)

    candidates: list[str] = []
    seen: set[str] = set()

    for env_name in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        base = merged_env.get(env_name)
        if not base:
            continue
        append_unique_candidate(candidates, seen, str(Path(base) / "NASM" / "nasm.exe"))

    append_unique_candidate(candidates, seen, r"C:\Program Files\NASM\nasm.exe")
    append_unique_candidate(candidates, seen, r"C:\Program Files (x86)\NASM\nasm.exe")
    return candidates


def macos_nasm_probe_candidates() -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    for prefix in ("/opt/homebrew", "/usr/local"):
        for tool in ("nasm", "yasm"):
            append_unique_candidate(candidates, seen, str(Path(prefix) / "bin" / tool))
            append_unique_candidate(candidates, seen, str(Path(prefix) / "opt" / tool / "bin" / tool))
    return candidates


def resolve_nasm_executable(env: Mapping[str, str] | None = None, *, platform_os: str | None = None) -> str | None:
    search_path = None
    if env is not None:
        search_path = env.get("PATH") or os.environ.get("PATH", "")

    override_names = [
        "NASM_EXECUTABLE",
        "CMAKE_ASM_NASM_COMPILER",
        "NASM",
        "YASM_EXECUTABLE",
        "YASM",
    ]
    for name in override_names:
        value = env.get(name) if env is not None else os.environ.get(name)
        resolved = resolve_executable_candidate(value, search_path=search_path)
        if resolved:
            return resolved

    for candidate in ("nasm", "yasm"):
        resolved = resolve_executable_candidate(candidate, search_path=search_path)
        if resolved:
            return resolved

    if platform_os == "windows":
        for candidate in windows_nasm_probe_candidates(env):
            resolved = resolve_executable_candidate(candidate, search_path=search_path)
            if resolved:
                return resolved
    elif platform_os == "macos":
        for candidate in macos_nasm_probe_candidates():
            resolved = resolve_executable_candidate(candidate, search_path=search_path)
            if resolved:
                return resolved

    return None
