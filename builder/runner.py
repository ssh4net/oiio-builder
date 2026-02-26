import os
import shlex
import subprocess
import sys
import _thread
import threading
from pathlib import Path


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

_OUTPUT_LOCK: _thread.LockType | None = None


def set_output_lock(lock: _thread.LockType | None) -> None:
    global _OUTPUT_LOCK
    _OUTPUT_LOCK = lock


def _locked_print(*args: object, **kwargs: object) -> None:
    lock = _OUTPUT_LOCK
    if lock is None:
        print(*args, **kwargs)
        return
    with lock:
        print(*args, **kwargs)


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
        _locked_print()
        _locked_print(f"{code}{line}{_ANSI_RESET}", flush=True)
        _locked_print(f"{code}{_ANSI_BOLD} {title}{_ANSI_RESET}", flush=True)
        _locked_print(f"{code}{line}{_ANSI_RESET}", flush=True)
        return

    _locked_print()
    _locked_print(line, flush=True)
    _locked_print(f" {title}", flush=True)
    _locked_print(line, flush=True)


def print_cmd(label: str, cmd: list[str]) -> None:
    text = format_cmd(cmd)
    if supports_color():
        _locked_print(f"{_ANSI_GRAY}{label}: {text}{_ANSI_RESET}", flush=True)
    else:
        _locked_print(f"{label}: {text}", flush=True)


def run(
    cmd: list[str],
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    log_path: str | None = None,
) -> None:
    if dry_run:
        _locked_print(f"[dry-run] {format_cmd(cmd)}")
        return
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    if not log_path:
        subprocess.run(cmd, cwd=cwd, env=merged_env, check=True)
        return

    log_file_path = Path(log_path)
    log_file_path.parent.mkdir(parents=True, exist_ok=True)

    header = f"$ {format_cmd(cmd)}\n"
    if cwd:
        header = f"$ (cd {cwd}) {format_cmd(cmd)}\n"

    with log_file_path.open("wb") as f:
        f.write(header.encode("utf-8", errors="replace"))
        f.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=merged_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        assert proc.stdout is not None

        stdout_buffer = getattr(sys.stdout, "buffer", None)
        for chunk in iter(proc.stdout.readline, b""):
            lock = _OUTPUT_LOCK
            if lock is not None:
                lock.acquire()
            try:
                if stdout_buffer is not None:
                    stdout_buffer.write(chunk)
                    stdout_buffer.flush()
                else:
                    sys.stdout.write(chunk.decode(errors="replace"))
                    sys.stdout.flush()
            finally:
                if lock is not None:
                    lock.release()
            f.write(chunk)

        ret = proc.wait()
        if ret != 0:
            lock = _OUTPUT_LOCK
            if lock is not None:
                lock.acquire()
            try:
                print(f"[error] Command failed (exit {ret}). Log: {log_file_path}", file=sys.stderr, flush=True)
            finally:
                if lock is not None:
                    lock.release()
            raise subprocess.CalledProcessError(ret, cmd)
