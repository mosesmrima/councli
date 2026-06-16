from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorktreeInfo:
    branch: str
    path: Path
    base_ref: str


def git(root: Path, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def require_git_repo(root: Path) -> None:
    proc = git(root, ["rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        raise RuntimeError(f"{root} is not a git repository")


def ensure_clean_enough(root: Path) -> None:
    proc = git(root, ["status", "--porcelain"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "could not read git status")
    dirty = [
        line
        for line in proc.stdout.splitlines()
        if line and not line.endswith(".councli/config.yaml") and not line.startswith("?? .councli/")
    ]
    if dirty:
        raise RuntimeError(
            "working tree has existing changes. Commit/stash them or rerun with --allow-dirty."
        )


def current_ref(root: Path) -> str:
    proc = git(root, ["branch", "--show-current"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    proc = git(root, ["rev-parse", "--short", "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "could not determine current ref")
    return proc.stdout.strip()


def current_commit(root: Path) -> str:
    proc = git(root, ["rev-parse", "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "could not determine current commit")
    return proc.stdout.strip()


def create_worktree(root: Path, *, run_name: str, executor: str) -> WorktreeInfo:
    require_git_repo(root)
    base_ref = current_commit(root)
    safe_run = slug(run_name)
    safe_executor = slug(executor)
    branch = f"councli/{safe_run}/{safe_executor}"
    base = root.parent / ".councli-worktrees" / root.name
    path = base / f"{safe_run}-{safe_executor}"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        raise RuntimeError(f"worktree path already exists: {path}")

    proc = git(root, ["worktree", "add", "-b", branch, str(path)])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git worktree add failed")
    return WorktreeInfo(branch=branch, path=path, base_ref=base_ref)


def list_worktrees(root: Path) -> dict[Path, dict[str, str]]:
    proc = git(root, ["worktree", "list", "--porcelain"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "could not list git worktrees")
    worktrees: dict[Path, dict[str, str]] = {}
    current_path: Path | None = None
    current: dict[str, str] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            if current_path is not None:
                worktrees[current_path] = current
            current_path = None
            current = {}
            continue
        if line.startswith("worktree "):
            if current_path is not None:
                worktrees[current_path] = current
            current_path = Path(line.removeprefix("worktree "))
            current = {}
            continue
        if current_path is None:
            continue
        if line.startswith("branch "):
            branch = line.removeprefix("branch ")
            current["branch"] = branch.removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current["head"] = line.removeprefix("HEAD ")
        elif line == "bare":
            current["bare"] = "true"
        elif line == "detached":
            current["detached"] = "true"
    if current_path is not None:
        worktrees[current_path] = current
    return worktrees


def remove_worktree(root: Path, path: Path) -> subprocess.CompletedProcess[str]:
    return git(root, ["worktree", "remove", "--force", str(path)], timeout=120)


def diff(root: Path, *, base_ref: str | None = None) -> str:
    args = ["diff", "--binary"]
    if base_ref:
        args.append(base_ref)
    proc = git(root, args)
    body = proc.stdout if proc.returncode == 0 else proc.stderr
    untracked = untracked_patch(root)
    return f"{body}\n\n{untracked}".strip() + "\n"


def apply_unified_diff(root: Path, patch: str, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    args = ["apply", "--whitespace=nowarn"]
    if check:
        args.append("--check")
    return subprocess.run(
        ["git", *args],
        cwd=root,
        input=patch,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def untracked_patch(root: Path) -> str:
    proc = git(root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if proc.returncode != 0 or not proc.stdout:
        return ""
    patches: list[str] = []
    for raw_name in proc.stdout.split("\0"):
        name = raw_name.strip()
        if not name:
            continue
        patch = git(
            root,
            ["diff", "--no-index", "--binary", "--", "/dev/null", name],
        )
        if patch.stdout:
            patches.append(patch.stdout.rstrip())
        elif patch.stderr:
            patches.append(f"Untracked file: {name}\n{patch.stderr.strip()}")
        else:
            patches.append(f"Untracked file: {name}")
    return "\n\n".join(patches)


def slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")
    return cleaned[:80] or "run"
