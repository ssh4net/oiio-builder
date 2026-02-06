import os
import shlex
import subprocess
import sys
from typing import Iterable


def format_cmd(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(part) for part in cmd)


_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_CYAN = "\033[36m"
_ANSI_GREEN = "\033[32m"
_ANSI_RED = "\033[31m"
_ANSI_YELLOW = "\033[33m"
_ANSI_GRAY = "\033[90m"

_ANSI_ENABLED = False


def _enable_windows_ansi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return


def supports_color() -> bool:
    global _ANSI_ENABLED
    if os.environ.get("NO_COLOR") is not None:
        return False
    if not sys.stdout.isatty():
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False
    if os.name == "nt" and not _ANSI_ENABLED:
        _enable_windows_ansi()
        _ANSI_ENABLED = True
    return True


def _ansi_color(name: str) -> str:
    match name:
        case "cyan":
            return _ANSI_CYAN
        case "green":
            return _ANSI_GREEN
        case "red":
            return _ANSI_RED
        case "gray":
            return _ANSI_GRAY
        case _:
            return _ANSI_YELLOW


def banner(title: str, *, color: str = "yellow", width: int = 54) -> None:
    line = "=" * width
    if supports_color():
        code = _ansi_color(color)
        print()
        print(f"{code}{line}{_ANSI_RESET}", flush=True)
        print(f"{code}{_ANSI_BOLD} {title}{_ANSI_RESET}", flush=True)
        print(f"{code}{line}{_ANSI_RESET}", flush=True)
        return

    print()
    print(line, flush=True)
    print(f" {title}", flush=True)
    print(line, flush=True)


def print_cmd(label: str, cmd: list[str]) -> None:
    text = format_cmd(cmd)
    if supports_color():
        print(f"{_ANSI_GRAY}{label}: {text}{_ANSI_RESET}", flush=True)
    else:
        print(f"{label}: {text}", flush=True)


def run(cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[dry-run] {format_cmd(cmd)}")
        return
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    subprocess.run(cmd, cwd=cwd, env=merged_env, check=True)
