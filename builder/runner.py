import os
import shlex
import subprocess
from typing import Iterable


def format_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run(cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[dry-run] {format_cmd(cmd)}")
        return
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    subprocess.run(cmd, cwd=cwd, env=merged_env, check=True)
