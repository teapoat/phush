"""Repository clones on the server, and the git flows the tools are built from.

The service keeps one working clone per repository under a single root directory. A
clone is created on first use and reused afterwards, so a chat client only ever sends
a repository name.

Every path the caller supplies is checked before it is touched: no absolute paths, no
"..", nothing inside .git, and the resolved path must still be inside the clone. That
check is `safe_join`, kept separate so it can be tested on its own.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from . import gitcmd
from .config import Config

log = logging.getLogger("phush.workspace")

# GitHub repository names: letters, digits, dot, dash, underscore. Must not start with
# a dash or dot, so a name can never be read as a git flag or a relative path.
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class WorkspaceError(RuntimeError):
    """The request was refused before any git or file operation ran."""


def safe_join(root: Path, relative: str) -> Path:
    """Resolve `relative` inside `root`, or raise WorkspaceError.

    Refuses absolute paths, parent traversal, and anything under .git — including via
    a symlink, because the final resolved path must still live inside root.
    """
    text = (relative or "").strip()
    if not text:
        raise WorkspaceError("empty path")
    candidate = Path(text)
    if candidate.is_absolute():
        raise WorkspaceError(f"absolute paths are not allowed: {text}")
    parts = candidate.parts
    if any(part == ".." for part in parts):
        raise WorkspaceError(f"path may not step outside the repository: {text}")
    if any(part == ".git" for part in parts):
        raise WorkspaceError(f"the .git directory is off limits: {text}")

    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise WorkspaceError(f"path escapes the repository: {text}")
    return resolved


@dataclass(frozen=True)
class RepoState:
    name: str
    path: Path
    branch: str
    cloned_now: bool


class Workspace:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.root = config.root

    # ---- naming and location -------------------------------------------------

    def check_name(self, repo: str) -> str:
        name = (repo or "").strip()
        if not REPO_NAME_RE.match(name):
            raise WorkspaceError(f"not a valid repository name: {repo!r}")
        if not self.config.repo_allowed(name):
            raise WorkspaceError(f"repository is not on this server's allow list: {name}")
        return name

    def repo_path(self, repo: str) -> Path:
        return self.root / self.check_name(repo)

    def remote_url(self, repo: str) -> str:
        return self.config.remote_template.format(owner=self.config.owner, repo=repo)

    def list_clones(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if (p / ".git").is_dir()
        )

    # ---- git flows -----------------------------------------------------------

    def _git(self, cwd: Path | None, args: list[str], check: bool = False):
        return gitcmd.run(
            cwd, args, timeout=self.config.git_timeout_seconds, check=check
        )

    def current_branch(self, path: Path) -> str:
        result = self._git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
        branch = result.stdout.strip()
        return branch or self.config.default_branch

    def open(self, repo: str) -> RepoState:
        """Make sure the clone exists and is up to date with the remote."""
        name = self.check_name(repo)
        path = self.repo_path(name)
        cloned_now = False

        if not (path / ".git").is_dir():
            self.root.mkdir(parents=True, exist_ok=True)
            log.info("cloning %s", name)
            self._git(
                None,
                ["clone", "--quiet", self.remote_url(name), str(path)],
                check=True,
            )
            self._git(path, ["config", "user.name", self.config.author_name], check=True)
            self._git(
                path, ["config", "user.email", self.config.author_email], check=True
            )
            cloned_now = True

        branch = self.current_branch(path)
        if not cloned_now:
            self.sync(path, branch)
        return RepoState(name=name, path=path, branch=branch, cloned_now=cloned_now)

    def sync(self, path: Path, branch: str) -> str:
        """Fast-forward the clone to the remote. Never discards local work."""
        fetch = self._git(path, ["fetch", "--quiet", "origin"])
        if not fetch.ok:
            return f"could not reach the remote: {fetch.text}"
        pull = self._git(path, ["pull", "--ff-only", "--quiet", "origin", branch])
        if pull.ok:
            return "up to date with the remote"
        # A dirty tree or local commits stop a fast-forward. Neither is an error here:
        # commit_push rebases local commits, and uncommitted edits are the caller's.
        return f"not fast-forwarded (local changes present): {pull.text}"

    def is_dirty(self, path: Path) -> bool:
        return bool(self._git(path, ["status", "--porcelain"]).stdout.strip())

    def commit_push(
        self, repo: str, message: str, paths: list[str] | None = None
    ) -> str:
        """Stage, commit, rebase onto the remote, and push. Reports what happened."""
        text = (message or "").strip()
        if not text:
            raise WorkspaceError("a commit message is required")

        state = RepoState(
            name=self.check_name(repo),
            path=self.repo_path(repo),
            branch="",
            cloned_now=False,
        )
        path = state.path
        if not (path / ".git").is_dir():
            raise WorkspaceError(f"repository is not open on this server: {repo}")
        branch = self.current_branch(path)

        if paths:
            rel = [str(safe_join(path, p).relative_to(path.resolve())) for p in paths]
            self._git(path, ["add", "--"] + rel, check=True)
        else:
            self._git(path, ["add", "--all"], check=True)

        staged = self._git(path, ["diff", "--cached", "--name-only"]).stdout.strip()
        if not staged:
            return "nothing to commit — the working tree matches the last commit"

        commit = self._git(path, ["commit", "--quiet", "-m", text])
        if not commit.ok:
            raise WorkspaceError(f"commit failed: {commit.text}")

        fetch = self._git(path, ["fetch", "--quiet", "origin"])
        if not fetch.ok:
            return (
                f"committed locally, but the remote is unreachable, so nothing was "
                f"pushed: {fetch.text}"
            )

        rebase = self._git(path, ["pull", "--rebase", "--quiet", "origin", branch])
        if not rebase.ok:
            self._git(path, ["rebase", "--abort"])
            return (
                "committed locally, but the remote has changes that conflict with it. "
                "The rebase was aborted and your commit is intact on the server. "
                f"Conflict: {rebase.text}"
            )

        push = self._git(path, ["push", "--quiet", "origin", branch])
        if not push.ok:
            return f"committed locally, but the push failed: {push.text}"

        head = self._git(path, ["log", "-1", "--format=%h %s"]).stdout.strip()
        files = "\n".join(f"  {name}" for name in staged.splitlines())
        return f"pushed to {branch}: {head}\n{files}"

    def status(self, repo: str) -> str:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            return f"repository is not open on this server: {repo}"
        branch = self.current_branch(path)
        dirty = self._git(path, ["status", "--short", "--branch"]).text
        return f"branch {branch}\n{dirty}"

    def log(self, repo: str, count: int = 10) -> str:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            return f"repository is not open on this server: {repo}"
        limit = max(1, min(int(count), 50))
        return self._git(
            path, ["log", f"-{limit}", "--format=%h %ad %s", "--date=short"]
        ).text

    def tracked_files(self, repo: str, subdir: str = "") -> list[str]:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            raise WorkspaceError(f"repository is not open on this server: {repo}")
        args = ["ls-files"]
        if subdir.strip():
            args += ["--", str(safe_join(path, subdir).relative_to(path.resolve()))]
        return self._git(path, args).stdout.splitlines()
