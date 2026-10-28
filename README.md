# phush

A small MCP server that lets a chat client edit files in your GitHub repositories
**without sending whole files through the model**.

## Why

A chat client that edits GitHub through the plain API has to send the file's full new
content on every write — the API has no patch mode. Changing one line of a 40 KB
document means the model regenerates all 40 KB. On a phone that is a minute of waiting
and a lot of tokens; on a 300 KB spreadsheet it is far worse.

phush moves the work to a server. It keeps a normal git clone of each repository, and
its tools take *the change*, not the file:

```
patch_file(repo="notes", path="TODO.md", old_text="- [ ] buy milk", new_text="- [x] buy milk")
commit_push(repo="notes", message="tick off the milk")
```

The traffic is a few hundred bytes, whatever the file's size. Cloning, pulling,
committing and pushing all happen on the server, exactly as they would in a terminal.

You do not need this on a machine that has a shell — use git directly there. It earns
its place on surfaces that have no shell at all.

## Tools

| Tool | What it does |
| --- | --- |
| `repo_open` | Clone or safely fast-forward; report blocked synchronization honestly |
| `repo_list` | List repositories already cloned on the server |
| `repo_status` | Branch and uncommitted changes |
| `repo_log` | Recent commits, or only the local / remote side |
| `repo_diff` | Worktree, index, HEAD, or either side of cached origin; literal paths and pagination |
| `repo_show` | Commit metadata and patch by commit id |
| `repo_sync` | Explicit fetch, check, fast-forward or merge with a recovery branch |
| `list_files` | Files git tracks, optionally under one directory |
| `read_file` | Read a file as numbered lines, with offset and limit |
| `search` | Regular-expression search across tracked files |
| `patch_file` | Replace exact text — the cheap edit |
| `append_file` | Add text to the end of a file |
| `write_file` | Replace a whole file (use `patch_file` when you can) |
| `xlsx_sheets` | Sheets of a spreadsheet, with a warning about what a save could drop |
| `xlsx_read` | Read a cell or range, for example `Q1:S20` |
| `xlsx_find` | Find cells containing some text |
| `xlsx_set` | Write values into named cells |
| `commit_push` | Commit selected work, safely merge if needed, and push; retry existing commits |

The spreadsheet tools exist for the same reason as `patch_file`: a 400 KB workbook is
not something to move through a model to change three cells. Note that saving a
workbook rewrites it — values, formulas and ordinary formatting survive, but charts,
images and pivot tables may not. `xlsx_sheets` says what is at risk before you write.

## Inspecting and synchronizing a clone

Ordinary reads and edits do not silently fetch or pull an existing clone. `repo_status`
includes the HEAD id, cached remote id, ahead/behind counts and uncommitted paths.
To refresh the remote information without integrating anything:

```
repo_sync(repo="notes", mode="check")
repo_log(repo="notes", side="local")
repo_log(repo="notes", side="remote")
repo_diff(repo="notes", scope="local-only", paths=["TODO.md"])
repo_diff(repo="notes", scope="remote-only", paths=["TODO.md"])
repo_show(repo="notes", commit="a1b2c3d")
```

Diff scopes are `worktree` (unstaged tracked files), `index` (staged), `head` (all
local edits), `remote` (HEAD to cached origin), and `local-only` / `remote-only`
(each side against their common ancestor). Untracked paths appear in status; use
`read_file` to inspect their contents. Diff and show accept one-based `offset` and
`limit` (at most 400 lines); output is also capped at 20,000 characters. Truncation
is explicit. Narrow the paths or line window if a long line hits the cap.

`repo_sync(mode="ff-only")` integrates a clean clone only when it can fast-forward.
`repo_sync(mode="merge")` may join diverged histories, keeping both sets of commit
ids. Every integration creates a `phush-backup/...` branch at the previous HEAD.
Unrelated histories and other Git operations block synchronization. Dirty work is
left untouched: first review it and commit selected paths. No stash, reset or
automatic choice of one side is performed.

A merge conflict is left in place for inspection, with the affected paths reported.
Read the conflict, use `patch_file` to resolve it, and finish with
`commit_push(repo="notes", message="Resolve both changes", paths=["TODO.md"])`.
Remaining conflict markers or unresolved paths block completion. Other staged work
blocks a named-path commit outside a merge, so it cannot silently join that commit.
`commit_push(..., paths=[])` retries previously created commits without staging or
committing new work. A rejected push leaves the commits and edits on the server.

## What it will not do

The server is reachable over the network, so it is built to make destruction
impossible rather than merely discouraged:

- git runs as an argv list, never through a shell;
- only a short list of git subcommands is allowed, and arguments that rewrite or
  destroy history (`--force`, `--hard`, `--amend`, `--delete`, `--prune`, force
  refspecs) are refused;
- repository hooks are disabled on every call, so a clone cannot run code;
- paths are checked before use: no absolute paths, no `..`, nothing inside `.git`, and
  the resolved path must still be inside the clone;
- a push is always a fast-forward of the current branch; integration uses merge,
  preserving existing commit ids; on conflict nothing is pushed;
- every call is logged and rate limited.

The worst outcome of a stolen token is an unwanted commit, which one `git revert`
undoes. Work that was already pushed cannot be lost.

## Install

Requires Python 3.11+ and git.

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml    # set owner, root, port
cp .env.example .env                  # set PHUSH_TOKEN, then chmod 600 .env
venv/bin/python -m phush
```

The server authenticates to GitHub with the machine's own SSH key. Create one with
`ssh-keygen -t ed25519` and add the public half to the account that owns the
repositories (Settings → SSH and GPG keys). No API token is involved.

## Serving it over HTTPS

phush binds to localhost and speaks MCP over Streamable HTTP. Put a reverse proxy in
front of it to terminate TLS and to supply the bearer token, so the client only needs a
URL. With nginx:

```nginx
location /your-secret-path/ {
    proxy_pass http://127.0.0.1:8766/;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:8766;
    proxy_set_header Authorization "Bearer your-token";
    proxy_set_header Connection "";
    proxy_buffering off;          # Streamable HTTP: do not buffer
    proxy_read_timeout 3600s;
}
location / { return 404; }
```

Treat the secret path as a password: it appears in access logs and in browser history.
Rotate it by editing the vhost and `.env`, then reloading both.

## Tests

```sh
python3 -m unittest discover tests
```

The tests build throwaway git repositories in a temporary directory, so they need git
but no network and no configuration.
