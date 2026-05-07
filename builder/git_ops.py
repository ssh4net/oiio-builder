from __future__ import annotations

from pathlib import Path
import subprocess

from .runner import run


_DEFAULT_BRANCH_RENAMES = {
    "master": "main",
    "main": "master",
}


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


def _git_success(path: Path, args: list[str]) -> bool:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


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


def _run_git_update(cmd: list[str], *, dry_run: bool) -> None:
    try:
        run(cmd, dry_run=dry_run)
    except subprocess.CalledProcessError:
        if dry_run or "--quiet" not in cmd:
            raise
        noisy_cmd = [part for part in cmd if part != "--quiet"]
        print(f"[note] Git command failed; retrying without --quiet: {' '.join(noisy_cmd)}", flush=True)
        run(noisy_cmd, dry_run=False)


def _build_fetch_cmd(path: Path, remote: str | None, ref: str | None, ref_type: str) -> list[str]:
    cmd = ["git", "-C", str(path), "fetch", "--quiet", "--prune"]
    if remote:
        cmd.append(remote)
    else:
        cmd.append("--all")

    if ref and ref_type == "tag":
        # Avoid unrelated retagged upstream tags breaking updates for branch/commit repos.
        if remote:
            cmd.extend(["--force", "tag", ref])
        else:
            cmd.append("--tags")

    return cmd


def _has_tracked_changes(path: Path) -> bool:
    return bool(_git_lines(path, ["status", "--porcelain", "--untracked-files=no"]))


def _current_branch(path: Path) -> str | None:
    branch = _git_output(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        return None
    return branch


def _local_branch_exists(path: Path, branch: str) -> bool:
    return _git_output(path, ["show-ref", "--verify", f"refs/heads/{branch}"]) is not None


def _remote_branch_exists(path: Path, remote: str, branch: str) -> bool:
    return _git_output(path, ["show-ref", "--verify", f"refs/remotes/{remote}/{branch}"]) is not None


def _is_ancestor(path: Path, ancestor: str, descendant: str) -> bool:
    return _git_success(path, ["merge-base", "--is-ancestor", ancestor, descendant])


def _tracking_ref(path: Path, remote: str | None) -> str | None:
    upstream = _git_output(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    if upstream:
        return upstream

    branch = _current_branch(path)
    if branch and remote and _remote_branch_exists(path, remote, branch):
        return f"{remote}/{branch}"
    return None


def _has_local_commits(path: Path, remote: str | None) -> bool:
    tracking_ref = _tracking_ref(path, remote)
    if not tracking_ref:
        return False
    return not _is_ancestor(path, "HEAD", tracking_ref)


def _repair_default_branch_rename(path: Path, remote: str | None, *, dry_run: bool) -> None:
    if not remote:
        return

    branch = _current_branch(path)
    if not branch:
        return

    replacement = _DEFAULT_BRANCH_RENAMES.get(branch)
    if not replacement:
        return

    if _remote_branch_exists(path, remote, branch):
        return
    if not _remote_branch_exists(path, remote, replacement):
        return

    remote_ref = f"{remote}/{replacement}"
    target_ref = replacement if _local_branch_exists(path, replacement) else "HEAD"
    if not _is_ancestor(path, target_ref, remote_ref):
        print(
            f"[skip-update] {path}: {remote}/{branch} is gone, but {target_ref} cannot fast-forward to {remote_ref}",
            flush=True,
        )
        return

    print(f"[note] {path}: {remote}/{branch} is gone; switching to {remote_ref}", flush=True)
    if _local_branch_exists(path, replacement):
        run(["git", "-C", str(path), "switch", replacement], dry_run=dry_run)
    else:
        run(["git", "-C", str(path), "branch", "-m", branch, replacement], dry_run=dry_run)
    run(["git", "-C", str(path), "branch", "--set-upstream-to", remote_ref, replacement], dry_run=dry_run)


def _build_current_branch_pull_cmd(path: Path, remote: str | None) -> list[str] | None:
    branch = _current_branch(path)
    if not branch:
        return None

    upstream = _git_output(path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])
    cmd = ["git", "-C", str(path), "pull", "--quiet", "--ff-only"]
    if upstream:
        return cmd

    if remote:
        cmd.extend([remote, branch])
        return cmd
    return None


def ensure_repo(path: Path, url: str | None, ref: str | None, ref_type: str, update: bool, dry_run: bool) -> None:
    if path.exists():
        if not (path / ".git").exists():
            return
        if not update:
            return
        remote = _select_remote(path, url, ref, ref_type)
        fetch_cmd = _build_fetch_cmd(path, remote, ref, ref_type)
        _run_git_update(fetch_cmd, dry_run=dry_run)
        if _has_tracked_changes(path):
            print(
                f"[skip-update] {path}: tracked local changes present; fetched remotes but skipped checkout/pull",
                flush=True,
            )
            return
        if ref:
            run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
            if _has_local_commits(path, remote):
                print(
                    f"[skip-update] {path}: local commits present; fetched remotes but skipped pull",
                    flush=True,
                )
                return
            if ref_type == "branch":
                pull_cmd = ["git", "-C", str(path), "pull", "--quiet", "--ff-only"]
                if remote:
                    pull_cmd.extend([remote, ref])
                _run_git_update(pull_cmd, dry_run=dry_run)
        else:
            _repair_default_branch_rename(path, remote, dry_run=dry_run)
            if _has_local_commits(path, remote):
                print(
                    f"[skip-update] {path}: local commits present; fetched remotes but skipped pull",
                    flush=True,
                )
                return
            pull_cmd = _build_current_branch_pull_cmd(path, remote)
            if pull_cmd:
                _run_git_update(pull_cmd, dry_run=dry_run)
        return

    if not url:
        raise RuntimeError(f"Missing url for repo at {path}")
    run(["git", "clone", url, str(path)], dry_run=dry_run)
    if ref:
        run(["git", "-C", str(path), "checkout", ref], dry_run=dry_run)
