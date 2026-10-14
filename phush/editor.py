"""File operations on a clone: read, search, and edit without moving whole files.

This is the point of the whole service. A chat model that wants to change one line of
a 40 KB file should send that one line, not the file. `patch_file` takes the exact text
to replace and the text to put in its place, which is a few hundred bytes even when the
file is huge.

Every function works on an already-validated absolute path (see workspace.safe_join)
and refuses to guess: if the text to replace is missing, or appears more times than the
caller expected, nothing is written and the caller is told what was actually found.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class EditError(RuntimeError):
    """The edit was refused. Nothing was written."""


def read_text(path: Path, max_bytes: int, limit_name: str = "read") -> str:
    if not path.is_file():
        raise EditError(f"no such file: {path.name}")
    size = path.stat().st_size
    if size > max_bytes:
        raise EditError(
            f"file is {size} bytes, over this server's {max_bytes}-byte {limit_name} "
            f"limit — use search, or read a line range instead"
        )
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise EditError("file is not UTF-8 text (binary files are not editable)") from exc


def read_lines(path: Path, max_bytes: int, offset: int = 1, limit: int = 400) -> str:
    """Return numbered lines, the way a terminal `cat -n` would show them."""
    text = read_text(path, max_bytes)
    lines = text.splitlines()
    start = max(1, int(offset))
    end = min(len(lines), start + max(1, int(limit)) - 1)
    if start > len(lines):
        return f"(file has {len(lines)} lines; line {start} is past the end)"
    body = "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, end + 1))
    tail = "" if end == len(lines) else f"\n… {len(lines) - end} more lines"
    return body + tail


def sha256(path: Path) -> str:
    """Checksum of any file, text or binary.

    Some projects record a file's checksum elsewhere in the repository and check it
    later. Editing such a file leaves that record stale, and a client with no shell
    cannot recompute it — hence this tool.
    """
    if not path.is_file():
        raise EditError(f"no such file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"{digest.hexdigest()}  {path.name} ({path.stat().st_size} bytes)"


def _looks_like_text(path: Path) -> bool:
    """Guess from the first few KB, the way `grep` and `file` do: no NUL bytes."""
    try:
        with path.open("rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return b"\x00" not in head


@dataclass(frozen=True)
class Match:
    path: str
    line_number: int
    line: str


@dataclass(frozen=True)
class GrepResult:
    """What a search found, and what it did not look at."""

    matches: list[Match]
    skipped_too_big: list[str]
    hit_match_limit: bool
    excluded: int = 0


def grep(
    root: Path,
    files: list[str],
    pattern: str,
    max_matches: int,
    max_bytes: int,
    exclude: Sequence[str] = (),
) -> GrepResult:
    """Search tracked files for a regular expression. Binary files are skipped.

    A file over the read limit is skipped too, but its name is reported: a caller
    told only "no matches" cannot tell a word that is absent from a file that was
    never searched, and would draw the wrong conclusion from the silence.

    `exclude` holds glob patterns the owner has declared not worth searching —
    saved raw dumps, exports, anything kept as an archive rather than read. A search
    opens every tracked file, so this is what keeps an archive from costing a
    repository's every query; unlike the size cap, it is a deliberate choice, so
    the answer reports only how many paths it covered, not their names.
    """
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise EditError(f"bad search pattern: {exc}") from exc

    found: list[Match] = []
    skipped: list[str] = []
    excluded = 0
    for name in files:
        if any(fnmatch.fnmatch(name, pattern_) for pattern_ in exclude):
            excluded += 1
            continue
        target = root / name
        if not target.is_file():
            continue
        if target.stat().st_size > max_bytes:
            # Only a text file is worth reporting: an image or archive could not have
            # matched anyway, and in a repo that stores raw dumps there are hundreds.
            if _looks_like_text(target):
                skipped.append(name)
            continue
        try:
            text = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                found.append(Match(name, number, line.rstrip()))
                if len(found) >= max_matches:
                    return GrepResult(found, skipped, True, excluded)
    return GrepResult(found, skipped, False, excluded)


def patch(path: Path, old: str, new: str, expected: int, max_bytes: int) -> str:
    """Replace exact text. Refuses when the count does not match `expected`.

    `max_bytes` here is the in-place edit limit, not the read limit: the payload is a
    snippet, so a file too big to serve as a whole read can still be patched.
    """
    if not old:
        raise EditError("old_text is empty — use append_file or write_file instead")
    if old == new:
        raise EditError("old_text and new_text are identical — nothing to do")

    text = read_text(path, max_bytes, limit_name="patch")
    count = text.count(old)
    if count == 0:
        raise EditError(
            "old_text was not found in the file — nothing was changed. Read the "
            "surrounding lines and copy the text exactly, including indentation."
        )
    want = max(1, int(expected))
    if count != want:
        raise EditError(
            f"old_text appears {count} times but replace_count is {want} — nothing was "
            f"changed. Add more surrounding text to make it unique, or set "
            f"replace_count to {count}."
        )

    path.write_text(text.replace(old, new), encoding="utf-8")
    return f"replaced {count} occurrence(s) in {path.name}"


def append(path: Path, text: str, max_write_bytes: int) -> str:
    if not text:
        raise EditError("nothing to append")
    if len(text.encode("utf-8")) > max_write_bytes:
        raise EditError("text is over this server's write limit")
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(existing + separator + text, encoding="utf-8")
    return f"appended {len(text)} characters to {path.name}"


def write(path: Path, content: str, max_write_bytes: int) -> str:
    if len(content.encode("utf-8")) > max_write_bytes:
        raise EditError("content is over this server's write limit")
    existed = path.is_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    verb = "overwrote" if existed else "created"
    return f"{verb} {path.name} ({len(content)} characters)"
