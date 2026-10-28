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
from uuid import uuid4

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
    ready: bool = True
    sync_report: str = "not requested"


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

    def open(self, repo: str, update: bool = True) -> RepoState:
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
        report = "cloned from remote" if cloned_now else "not requested"
        ready = True
        if not cloned_now and update:
            report = self.sync(path, branch)
            ready = report.startswith("synced:")
        return RepoState(name=name, path=path, branch=branch, cloned_now=cloned_now,
                         ready=ready, sync_report=report)

    def sync(self, path: Path, branch: str) -> str:
        """Opening may fast-forward, but never integrates divergent histories."""
        return self.repo_sync(path.name, "ff-only")

    def _path(self, repo: str) -> Path:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            raise WorkspaceError(f"repository is not open on this server: {repo}")
        return path

    def _refs(self, path: Path) -> tuple[str, str]:
        branch = self.current_branch(path)
        if branch == "HEAD" or not self._git(path, ["check-ref-format", f"refs/heads/{branch}"]).ok:
            raise WorkspaceError("a named branch is required")
        return branch, f"refs/remotes/origin/{branch}"

    def _resolve(self, path: Path, ref: str) -> str:
        if not (ref == "HEAD" or re.fullmatch(r"[0-9a-fA-F]{7,40}", ref)):
            raise WorkspaceError("commit must be HEAD or a hexadecimal commit id")
        return self._git(path, ["rev-parse", "--verify", "--end-of-options",
                                f"{ref}^{{commit}}"], check=True).stdout.strip()

    def _merge_pending(self, path: Path) -> bool:
        return self._git(path, ["rev-parse", "--verify", "MERGE_HEAD"]).ok

    def _conflicts(self, path: Path) -> str:
        return self._git(path, ["diff", "--name-only", "--diff-filter=U"],
                         check=True).stdout.strip()

    def _counts(self, path: Path, remote: str) -> tuple[int, int]:
        out = self._git(path, ["rev-list", "--left-right", "--count",
                               f"HEAD...{remote}"], check=True).stdout
        return tuple(map(int, out.split()))

    def _paths(self, path: Path, paths: list[str] | None) -> list[str]:
        return [":(literal)" + str(safe_join(path, p).relative_to(path.resolve()))
                for p in (paths or [])]

    @staticmethod
    def _page(text: str, offset: int = 1, limit: int = 200) -> str:
        start = max(0, int(offset) - 1)
        count = max(1, min(int(limit), 400))
        lines = text.splitlines()
        selected = lines[start:start + count]
        body = "\n".join(selected)
        if len(body) > 20000:
            return body[:20000] + "\n(output truncated at 20000 characters; narrow paths or limit)"
        end = start + len(selected)
        suffix = f"; next offset {end + 1}" if end < len(lines) else "; end"
        empty = "(no changes)" if not lines else "(no lines at this offset)"
        return (body or empty) + f"\n(lines {start + 1}-{end}{suffix})"

    def diff(self, repo: str, scope: str = "worktree", paths: list[str] | None = None,
             offset: int = 1, limit: int = 200) -> str:
        """Inspect cached refs and local changes without fetching or changing files."""
        path = self._path(repo)
        args = ["diff", "--no-ext-diff", "--no-textconv"]
        if scope == "index":
            args += ["--cached"]
        elif scope == "head":
            args += ["HEAD"]
        elif scope in ("remote", "local-only", "remote-only"):
            _, remote = self._refs(path)
            if scope == "remote":
                args += ["HEAD", remote]
            else:
                base = self._git(path, ["merge-base", "HEAD", remote], check=True).stdout.strip()
                args += [base, "HEAD" if scope == "local-only" else remote]
        elif scope != "worktree":
            raise WorkspaceError("unknown diff scope")
        args += ["--"] + self._paths(path, paths)
        return self._page(self._git(path, args, check=True).stdout, offset, limit)

    def show(self, repo: str, commit: str, paths: list[str] | None = None,
             offset: int = 1, limit: int = 200) -> str:
        path = self._path(repo)
        oid = self._resolve(path, commit)
        args = ["show", "--no-ext-diff", "--no-textconv", "--format=fuller",
                "--stat", "--patch", oid, "--"] + self._paths(path, paths)
        return self._page(self._git(path, args, check=True).stdout, offset, limit)

    def repo_sync(self, repo: str, mode: str = "check") -> str:
        """Fetch explicitly, then check, fast-forward, or merge with a recovery ref."""
        if mode not in ("check", "ff-only", "merge"):
            raise WorkspaceError("sync mode must be check, ff-only, or merge")
        path = self._path(repo)
        branch, remote = self._refs(path)
        if self._merge_pending(path):
            return "blocked: merge in progress; resolve and commit named paths\n" + self._conflicts(path)
        if any((path / ".git" / part).exists() for part in
               ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
            return "blocked: another Git operation is in progress; left untouched"
        available = self._git(path, ["ls-remote", "--exit-code", "--heads", "origin",
                                     f"refs/heads/{branch}"])
        if not available.ok:
            return ("blocked: fetch failed (remote or branch unavailable); local work intact: "
                    + available.text)
        fetch = self._git(path, ["fetch", "--quiet", "origin"])
        if not fetch.ok:
            return f"blocked: fetch failed (remote or branch unavailable); local work intact: {fetch.text}"
        exists = self._git(path, ["rev-parse", "--verify", remote])
        if not exists.ok:
            return f"blocked: remote branch origin/{branch} is missing; no changes made"
        ahead, behind = self._counts(path, remote)
        state = f"ahead {ahead} / behind {behind}; " + self.status(repo)
        if mode == "check":
            return "fetched; " + state
        if not self._git(path, ["merge-base", "HEAD", remote]).ok:
            return "blocked: unrelated histories; preserve and inspect both sides\n" + state
        if not behind:
            return "synced: remote fetched; " + state
        if self.is_dirty(path):
            return "blocked: dirty tree preserved; commit selected paths before sync\n" + state
        if ahead and mode == "ff-only":
            return "blocked: histories diverged; inspect diff and use explicit merge\n" + state
        backup = "phush-backup/" + uuid4().hex
        self._git(path, ["branch", backup, "HEAD"], check=True)
        args = (["merge", "--no-edit", "--no-ff", remote] if ahead
                else ["merge", "--ff-only", remote])
        merged = self._git(path, args)
        if not merged.ok:
            return (f"blocked: merge conflict or merge failure; backup {backup}; "
                    "nothing pushed. Inspect repo_diff, patch the conflict files, then "
                    "commit_push with explicit paths.\n" + self._conflicts(path) + "\n" + merged.text)
        return f"synced: backup {backup}; " + self.status(repo)

    def is_dirty(self, path: Path) -> bool:
        return bool(self._git(path, ["status", "--porcelain"]).stdout.strip())

    def commit_push(
        self, repo: str, message: str, paths: list[str] | None = None
    ) -> str:
        """Commit selected work, merge safely when needed, and retry pending pushes."""
        text = (message or "").strip()
        if not text:
            raise WorkspaceError("a commit message is required")

        path = self._path(repo)
        branch, remote = self._refs(path)
        if any((path / ".git" / part).exists() for part in
               ("rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD")):
            return "blocked: another Git operation is in progress; left untouched"
        pending = self._merge_pending(path)
        if pending and not paths:
            return "blocked: merge in progress; provide explicit resolved paths\n" + self._conflicts(path)

        if paths and not pending:
            staged_before = set(self._git(path, ["diff", "--cached", "--name-only", "-z"],
                                          check=True).stdout.split("\0")) - {""}
            selected = set(self._git(path, ["diff", "--cached", "--name-only", "-z", "--"]
                                     + self._paths(path, paths), check=True).stdout.split("\0"))
            if staged_before - selected:
                return "blocked: other paths were already staged; index left untouched"

        if paths is not None:
            if pending:
                for name in paths:
                    target = safe_join(path, name)
                    if target.is_file() and any(line.startswith(("<<<<<<< ", "||||||| ", "=======", ">>>>>>> "))
                                                for line in target.read_text(errors="replace").splitlines()):
                        return f"blocked: conflict markers remain in {name}; nothing staged"
            if paths:
                self._git(path, ["add", "--"] + self._paths(path, paths), check=True)
        else:
            self._git(path, ["add", "--all"], check=True)

        staged = self._git(path, ["diff", "--cached", "--name-only"]).stdout.strip()
        if self._conflicts(path):
            return "blocked: unresolved merge paths\n" + self._conflicts(path)
        committed = bool((staged or pending) and paths != [])
        if committed:
            commit = self._git(path, ["commit", "--quiet", "-m", text])
            if not commit.ok:
                raise WorkspaceError(f"commit failed: {commit.text}")

        report = self.repo_sync(repo, "merge")
        if not report.startswith("synced:"):
            return "local commits intact; " + report
        ahead, _ = self._counts(path, remote)
        if not ahead:
            return "nothing to commit or push; " + self.status(repo)

        push = self._git(path, ["push", "--quiet", "origin", branch])
        if not push.ok:
            return f"committed locally, but the push failed: {push.text}"

        head = self._git(path, ["log", "-1", "--format=%h %s"]).stdout.strip()
        files = "\n".join(f"  {name}" for name in staged.splitlines()) if committed else ""
        return f"pushed to {branch}: {head}\n{files}\n" + self.status(repo)

    def status(self, repo: str) -> str:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            return f"repository is not open on this server: {repo}"
        branch = self.current_branch(path)
        dirty = self._git(path, ["status", "--short", "--branch"]).text
        head = self._git(path, ["rev-parse", "HEAD"], check=True).stdout.strip()
        if branch == "HEAD":
            return f"detached HEAD {head}; sync requires a named branch\n{dirty}"
        _, remote = self._refs(path)
        origin = self._git(path, ["rev-parse", "--verify", remote])
        refs = f"HEAD {head}\norigin/{branch} {origin.stdout.strip()} (cached; repo_sync refreshes)"
        if origin.ok:
            ahead, behind = self._counts(path, remote)
            refs += f"\nahead {ahead} / behind {behind}"
        else:
            refs += "\nremote branch unavailable"
        conflicts = self._conflicts(path)
        if conflicts:
            refs += "\nunresolved paths:\n" + conflicts
        return f"branch {branch}\n{refs}\n{dirty}"

    def log(self, repo: str, count: int = 10, side: str = "head") -> str:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            return f"repository is not open on this server: {repo}"
        limit = max(1, min(int(count), 50))
        args = ["log", f"-{limit}", "--format=%h %ad %s", "--date=short"]
        if side in ("local", "remote"):
            _, remote = self._refs(path)
            args += [f"{remote}..HEAD" if side == "local" else f"HEAD..{remote}"]
        elif side != "head":
            raise WorkspaceError("log side must be head, local, or remote")
        return self._git(path, args, check=True).text or "(no commits on this side)"

    def tracked_files(self, repo: str, subdir: str = "") -> list[str]:
        path = self.repo_path(repo)
        if not (path / ".git").is_dir():
            raise WorkspaceError(f"repository is not open on this server: {repo}")
        args = ["ls-files"]
        if subdir.strip():
            args += ["--", str(safe_join(path, subdir).relative_to(path.resolve()))]
        return self._git(path, args).stdout.splitlines()
