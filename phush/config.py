"""Load settings from config.toml and the secret token from the environment.

Non-secret settings live in config.toml (see config.example.toml). The bearer token
lives in .env / the environment as PHUSH_TOKEN and is never read from the TOML file,
so the config can be committed while the secret stays out of git.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    owner: str
    remote_template: str
    root: Path
    default_branch: str
    allow_repos: frozenset[str]
    author_name: str
    author_email: str
    max_read_bytes: int
    max_patch_bytes: int
    search_exclude: tuple[str, ...]
    max_write_bytes: int
    max_grep_matches: int
    git_timeout_seconds: float
    rate_max_requests: int
    rate_window_seconds: float
    token: str

    def repo_allowed(self, repo: str) -> bool:
        """An empty allow list means every repo of the owner is reachable."""
        return not self.allow_repos or repo in self.allow_repos


def _config_path() -> Path:
    """config.toml location: PHUSH_CONFIG env, else config.toml in the current dir."""
    env = os.environ.get("PHUSH_CONFIG")
    if env:
        return Path(env)
    return Path("config.toml")


def load_config() -> Config:
    # Imported here, not at module level, so the rest of the package (and its tests)
    # stay usable without the dependency installed.
    from dotenv import load_dotenv

    load_dotenv()  # reads .env if present; real env vars still win

    path = _config_path()
    if not path.is_file():
        raise SystemExit(
            f"config file not found: {path}\n"
            "Copy config.example.toml to config.toml (or set PHUSH_CONFIG)."
        )
    with path.open("rb") as f:
        data = tomllib.load(f)

    server = data.get("server", {})
    git = data.get("git", {})
    search = data.get("search", {})
    limits = data.get("limits", {})
    rate = data.get("rate_limit", {})

    owner = str(git.get("owner", "")).strip()
    if not owner:
        raise SystemExit("config error: [git] owner is empty — refusing to start.")

    root = Path(str(git.get("root", "repos"))).expanduser()

    token = os.environ.get("PHUSH_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "PHUSH_TOKEN is not set. Put it in .env (see .env.example) — "
            "the server refuses to start without a token."
        )

    return Config(
        host=str(server.get("host", "127.0.0.1")),
        port=int(server.get("port", 8766)),
        owner=owner,
        remote_template=str(
            git.get("remote_template", "git@github.com:{owner}/{repo}.git")
        ),
        root=root,
        default_branch=str(git.get("default_branch", "main")),
        allow_repos=frozenset(git.get("allow_repos", [])),
        author_name=str(git.get("author_name", "phush")),
        author_email=str(git.get("author_email", "phush@localhost")),
        max_read_bytes=int(limits.get("max_read_bytes", 5_000_000)),
        max_patch_bytes=int(limits.get("max_patch_bytes", 5_000_000)),
        search_exclude=tuple(str(g) for g in search.get("exclude", [])),
        max_write_bytes=int(limits.get("max_write_bytes", 1_000_000)),
        max_grep_matches=int(limits.get("max_grep_matches", 100)),
        git_timeout_seconds=float(limits.get("git_timeout_seconds", 120)),
        rate_max_requests=int(rate.get("max_requests", 60)),
        rate_window_seconds=float(rate.get("window_seconds", 60)),
        token=token,
    )
