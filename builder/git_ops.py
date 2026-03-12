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


def _git_output(path: Path, args: list[str]) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return None


def _git_lines(path: Path, args: list[str]) -> list[str]:
    output = _git_output(path, args)
    if not output:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _select_remote(path: Path, url: str | None, ref: str | None, ref_type: str) -> str | None:
    remotes = _git_lines(path, ["remote"])
    if not remotes:
        return None

    if url:
        wanted = url.rstrip("/")
        for remote in remotes:
            remote_url = _git_output(path, ["remote", "get-url", remote])
            if remote_url and remote_url.rstrip("/") == wanted:
                return remote

    if ref and ref_type == "branch":
        tracked_remote = _git_output(path, ["config", f"branch.{ref}.remote"])
        if tracked_remote and tracked_remote in remotes:
            return tracked_remote

        current_branch = _git_output(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        if current_branch and current_branch != "HEAD":
            tracked_remote = _git_output(path, ["config", f"branch.{current_branch}.remote"])
            if tracked_remote and tracked_remote in remotes:
                return tracked_remote

    if "origin" in remotes:
        return "origin"
    return remotes[0]


def ensure_repo(path: Path, url: str | None, ref: str | None, ref_type: str, update: bool, dry_run: bool) -> None:
    if path.exists():
        if not (path / ".git").exists():
            return
        if not update:
            return
        remote = _select_remote(path, url, ref, ref_type)
        fetch_cmd = ["git", "-C", str(path), "fetch", "--quiet"]
        if remote:
            fetch_cmd.append(remote)
        else:
            fetch_cmd.append("--all")
        fetch_cmd.append("--tags")
        run(fetch_cmd, dry_run=dry_run)
        if ref:
            run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
            if ref_type == "branch":
                pull_cmd = ["git", "-C", str(path), "pull", "--quiet", "--ff-only"]
                if remote:
                    pull_cmd.extend([remote, ref])
                run(pull_cmd, dry_run=dry_run)
        return

    if not url:
        raise RuntimeError(f"Missing url for repo at {path}")
    run(["git", "clone", url, str(path)], dry_run=dry_run)
    if ref:
        run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
