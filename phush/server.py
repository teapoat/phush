"""The MCP server: tool definitions, bearer-token gate, and startup.

The tools mirror what a developer does in a terminal — open a repo, look at a file,
change part of it, commit, push — but each call carries only the part that changed.
That is the whole reason this server exists: a chat client editing a large file over
the plain GitHub API has to resend the entire file on every edit.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import deque

import uvicorn
from mcp.server import MCPServer

from . import editor, sheets
from .config import Config, load_config
from .editor import EditError
from .gitcmd import GitError
from .sheets import SheetError
from .workspace import Workspace, WorkspaceError, safe_join

log = logging.getLogger("phush")


class BearerAuthMiddleware:
    """Reject any request without the exact `Authorization: Bearer <token>` header.

    Written as plain ASGI (not Starlette BaseHTTPMiddleware) so it never buffers the
    streamed MCP responses — it only inspects request headers and either short-circuits
    with 401 or passes the request through untouched.
    """

    def __init__(self, app, token: str) -> None:
        self.app = app
        self._expected = f"Bearer {token}"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not secrets.compare_digest(auth, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized\n"})
            return
        await self.app(scope, receive, send)


class RateLimit:
    """A rolling window over recent calls. Makes a runaway loop or a leaked token
    obvious instead of expensive."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._calls: deque[float] = deque()

    def check(self) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.window:
            self._calls.popleft()
        if len(self._calls) >= self.max_requests:
            raise WorkspaceError(
                f"rate limit reached ({self.max_requests} calls per "
                f"{self.window:.0f}s) — try again shortly"
            )
        self._calls.append(now)


def build_server(config: Config) -> MCPServer:
    workspace = Workspace(config)
    limiter = RateLimit(config.rate_max_requests, config.rate_window_seconds)
    mcp = MCPServer("phush", version="0.1.0")

    def _open(repo: str, tool: str, detail: str = ""):
        """Shared entry for every tool: rate limit, log, make sure the clone is ready."""
        limiter.check()
        log.info("%s repo=%s %s", tool, repo, detail)
        return workspace.open(repo)

    @mcp.tool(
        description=(
            "Open a repository on the server: clone it if needed, then bring it up to "
            "date with the remote. Call this before reading or editing. Returns the "
            "branch and whether the clone was fresh."
        )
    )
    async def repo_open(repo: str) -> str:
        try:
            state = _open(repo, "repo_open")
        except (WorkspaceError, GitError) as exc:
            return f"refused: {exc}"
        what = "cloned for the first time" if state.cloned_now else "already on the server"
        return f"{state.name} ready on branch {state.branch} ({what})"

    @mcp.tool(
        description=(
            "List the repositories this server currently has a clone of. Any other "
            "repository of the configured owner can still be opened with repo_open."
        )
    )
    async def repo_list() -> str:
        clones = workspace.list_clones()
        if not clones:
            return "no repositories are cloned yet — use repo_open to add one"
        return "\n".join(clones)

    @mcp.tool(
        description="Show the branch and any uncommitted changes in a repository."
    )
    async def repo_status(repo: str) -> str:
        try:
            limiter.check()
            return workspace.status(repo)
        except (WorkspaceError, GitError) as exc:
            return f"refused: {exc}"

    @mcp.tool(description="Show the most recent commits of a repository.")
    async def repo_log(repo: str, count: int = 10) -> str:
        try:
            limiter.check()
            return workspace.log(repo, count)
        except (WorkspaceError, GitError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "List the files git tracks in a repository, optionally under one "
            "subdirectory. Untracked and ignored files are not shown."
        )
    )
    async def list_files(repo: str, subdir: str = "") -> str:
        try:
            _open(repo, "list_files", subdir)
            files = workspace.tracked_files(repo, subdir)
        except (WorkspaceError, GitError) as exc:
            return f"refused: {exc}"
        if not files:
            return "no tracked files found"
        return "\n".join(files)

    @mcp.tool(
        description=(
            "Read a text file from a repository, as numbered lines. Use offset and "
            "limit to read part of a large file instead of all of it."
        )
    )
    async def read_file(repo: str, path: str, offset: int = 1, limit: int = 400) -> str:
        try:
            state = _open(repo, "read_file", path)
            target = safe_join(state.path, path)
            return editor.read_lines(target, config.max_read_bytes, offset, limit)
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Search the tracked files of a repository for a regular expression. "
            "Returns matching lines with their file and line number — the cheap way "
            "to find the text you want to patch."
        )
    )
    async def search(repo: str, pattern: str, subdir: str = "") -> str:
        try:
            state = _open(repo, "search", pattern)
            files = workspace.tracked_files(repo, subdir)
            matches = editor.grep(
                state.path,
                files,
                pattern,
                config.max_grep_matches,
                config.max_read_bytes,
            )
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"
        if not matches:
            return "no matches"
        return "\n".join(f"{m.path}:{m.line_number}: {m.line}" for m in matches)

    @mcp.tool(
        description=(
            "Replace exact text inside a file. old_text must match the file byte for "
            "byte, including indentation, and must appear exactly replace_count times "
            "or nothing is changed. This is the cheap way to edit a large file: send "
            "only the part that changes. The edit is not pushed until commit_push."
        )
    )
    async def patch_file(
        repo: str,
        path: str,
        old_text: str,
        new_text: str,
        replace_count: int = 1,
    ) -> str:
        try:
            state = _open(repo, "patch_file", path)
            target = safe_join(state.path, path)
            return editor.patch(
                target, old_text, new_text, replace_count, config.max_read_bytes
            )
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Add text to the end of a file, creating it if needed. The file is not "
            "pushed until commit_push."
        )
    )
    async def append_file(repo: str, path: str, text: str) -> str:
        try:
            state = _open(repo, "append_file", path)
            target = safe_join(state.path, path)
            return editor.append(target, text, config.max_write_bytes)
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Write a whole file, replacing any existing content. Prefer patch_file for "
            "an existing file — this sends the entire content. The file is not pushed "
            "until commit_push."
        )
    )
    async def write_file(repo: str, path: str, content: str) -> str:
        try:
            state = _open(repo, "write_file", path)
            target = safe_join(state.path, path)
            return editor.write(target, content, config.max_write_bytes)
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Checksum (sha256) of any file in a repository, text or binary. Use it "
            "when a project records a file's checksum somewhere else and you have just "
            "changed that file."
        )
    )
    async def file_sha256(repo: str, path: str) -> str:
        try:
            state = _open(repo, "file_sha256", path)
            return editor.sha256(safe_join(state.path, path))
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "List the sheets of a spreadsheet (.xlsx) with their size, and warn about "
            "charts or images that a save could drop. Look here before writing cells."
        )
    )
    async def xlsx_sheets(repo: str, path: str) -> str:
        try:
            state = _open(repo, "xlsx_sheets", path)
            return sheets.describe(safe_join(state.path, path))
        except (WorkspaceError, GitError, SheetError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Read a cell or a rectangular range from a spreadsheet, for example "
            "'Q1:S20'. Empty cells are skipped. Leave sheet empty for the first sheet."
        )
    )
    async def xlsx_read(repo: str, path: str, cells: str, sheet: str = "") -> str:
        try:
            state = _open(repo, "xlsx_read", f"{path} {cells}")
            return sheets.read_range(safe_join(state.path, path), sheet, cells)
        except (WorkspaceError, GitError, SheetError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Find cells containing some text, to locate the row you want to edit. "
            "Returns cell addresses with their values."
        )
    )
    async def xlsx_find(repo: str, path: str, text: str, sheet: str = "") -> str:
        try:
            state = _open(repo, "xlsx_find", f"{path} {text}")
            return sheets.find(safe_join(state.path, path), sheet, text)
        except (WorkspaceError, GitError, SheetError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Write values into named cells of a spreadsheet, for example "
            "{'Q5': '42', 'R5': 'done'}. Text that looks like a number is stored as a "
            "number, and a value starting with '=' is stored as a formula. The file is "
            "not pushed until commit_push."
        )
    )
    async def xlsx_set(
        repo: str, path: str, values: dict[str, str], sheet: str = ""
    ) -> str:
        try:
            state = _open(repo, "xlsx_set", f"{path} {sorted(values or {})}")
            return sheets.set_cells(safe_join(state.path, path), sheet, values)
        except (WorkspaceError, GitError, SheetError) as exc:
            return f"refused: {exc}"

    @mcp.tool(
        description=(
            "Commit the pending changes and push them to the remote. Give a clear "
            "commit message. Pass paths to commit only those files, or leave it out to "
            "commit everything that changed. If the remote moved on, the commit is "
            "rebased on top of it; on conflict nothing is pushed and the commit stays "
            "on the server."
        )
    )
    async def commit_push(
        repo: str, message: str, paths: list[str] | None = None
    ) -> str:
        try:
            limiter.check()
            log.info("commit_push repo=%s message=%r", repo, message)
            return workspace.commit_push(repo, message, paths)
        except (WorkspaceError, GitError, EditError) as exc:
            return f"refused: {exc}"

    return mcp


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = load_config()
    mcp = build_server(config)

    app = mcp.streamable_http_app(host=config.host)
    app.add_middleware(BearerAuthMiddleware, token=config.token)

    log.info(
        "starting on %s:%d, owner=%s, clones in %s",
        config.host,
        config.port,
        config.owner,
        config.root,
    )
    uvicorn.run(app, host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
