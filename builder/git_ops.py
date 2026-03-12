from __future__ import annotations

from pathlib import Path
import subprocess

from .runner import run


def git_head(path: Path) -> str | None:
    git_dir = path / ".git"
    if not git_dir.exists():
        return None
    try:
        return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    except subprocess.CalledProcessError:
        return None


def ensure_repo(path: Path, url: str | None, ref: str | None, ref_type: str, update: bool, dry_run: bool) -> None:
    if path.exists():
        if not (path / ".git").exists():
            return
        if not update:
            return
        run(["git", "-C", str(path), "fetch", "--quiet", "--all", "--tags"], dry_run=dry_run)
        if ref:
            run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
            if ref_type == "branch":
                run(["git", "-C", str(path), "pull", "--quiet", "--ff-only"], dry_run=dry_run)
        return

    if not url:
        raise RuntimeError(f"Missing url for repo at {path}")
    run(["git", "clone", url, str(path)], dry_run=dry_run)
    if ref:
        run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
