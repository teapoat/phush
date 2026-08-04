"""A deliberately small, non-destructive wrapper around the git binary.

Two rules make this safe enough to expose to a chat client over the network:

1. Git is always called with an argv list, never through a shell. Nothing the caller
   sends can turn into a shell command.
2. Only a short list of subcommands is allowed, and every argument is screened against
   flags that rewrite or destroy published history (--force, --hard, --amend, --delete
   and friends). The caller never supplies raw git arguments anyway; this screen is a
   second line of defence in case a future tool is written carelessly.

The worst thing a caller can do through this module is add an unwanted commit, which
is undone with one `git revert`. It cannot lose work that was already pushed.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# Subcommands the service may run. Anything else is refused outright.
ALLOWED_SUBCOMMANDS = frozenset(
    {
        "add",
        "clone",
        "commit",
        "config",
        "diff",
        "fetch",
        "log",
        "ls-files",
        "pull",
        "push",
        "rebase",
        "rev-parse",
        "status",
        "symbolic-ref",
    }
)

# Arguments that could destroy or rewrite history. Refused wherever they appear.
FORBIDDEN_ARGS = frozenset(
    {
        "-f",
        "--force",
        "--force-with-lease",
        "--force-if-includes",
        "--hard",
        "--amend",
        "--delete",
        "--prune",
        "--mirror",
        "-D",
        "--allow-unrelated-histories",
        "--exec",
        "--upload-pack",
        "--receive-pack",
        "-c",
        "--config-env",
    }
)

# `git rebase --abort` is how we recover from a failed rebase, so it is carved out of
# the general ban on rebase subcommands touching history.
_REBASE_SAFE = frozenset({"--abort", "--quit"})


class GitError(RuntimeError):
    """A git call was refused before running, or failed while running."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        """Combined output, trimmed — what a human wants to read."""
        return (self.stdout + ("\n" + self.stderr if self.stderr else "")).strip()


def check_args(args: list[str]) -> None:
    """Raise GitError if this argv is not something the service may run."""
    if not args:
        raise GitError("empty git command")
    sub = args[0]
    if sub not in ALLOWED_SUBCOMMANDS:
        raise GitError(f"git subcommand not allowed: {sub}")
    for arg in args[1:]:
        if arg in _REBASE_SAFE and sub == "rebase":
            continue
        if arg in FORBIDDEN_ARGS:
            raise GitError(f"git argument not allowed: {arg}")
        # Refuse a refspec that force-pushes, e.g. "+main:main".
        if sub == "push" and arg.startswith("+"):
            raise GitError(f"force refspec not allowed: {arg}")
        # Refuse ref deletion by empty source, e.g. ":main".
        if sub == "push" and arg.startswith(":"):
            raise GitError(f"ref deletion not allowed: {arg}")


def run(
    cwd: Path | None,
    args: list[str],
    *,
    timeout: float = 120,
    check: bool = False,
) -> GitResult:
    """Run one git command and return its result.

    cwd is the repository directory (None only for `clone`, which creates it).
    """
    check_args(args)
    env_args = ["git", "-c", "core.hooksPath=/dev/null"]
    # NOTE: the -c above is ours, not the caller's — it disables repository hooks so a
    # cloned repo cannot run code on the server. check_args bans -c from callers.
    proc = subprocess.run(  # noqa: S603 - argv list, never a shell string
        env_args + args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = GitResult(proc.returncode, proc.stdout, proc.stderr)
    if check and not result.ok:
        raise GitError(f"git {args[0]} failed: {result.text}")
    return result
