from __future__ import annotations

from dataclasses import dataclass
import platform as _platform
import sys


@dataclass(frozen=True)
class PlatformInfo:
    os: str  # macos, linux, windows
    arch: str  # x86_64, arm64


def _normalize_arch(value: str) -> str:
    v = value.lower()
    if v in {"x86_64", "amd64"}:
        return "x86_64"
    if v in {"arm64", "aarch64"}:
        return "arm64"
    return v


def detect_platform() -> PlatformInfo:
    if sys.platform.startswith("darwin"):
        os_name = "macos"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    elif sys.platform.startswith("win"):
        os_name = "windows"
    else:
        os_name = sys.platform
    return PlatformInfo(os=os_name, arch=_normalize_arch(_platform.machine()))
