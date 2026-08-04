"""Spreadsheet cells, so a chat client never has to move a workbook around.

A .xlsx file is a few hundred kilobytes of binary. Sending it through a model to change
three cells is absurd, and re-uploading it costs as much again. These helpers do the
same thing `patch_file` does for text: the caller names cells and values, and the file
itself stays on the server.

Caveat worth knowing before using this on a workbook you care about: openpyxl rewrites
the whole file when it saves. Cell values, formulas and ordinary formatting survive;
charts, images, pivot tables and macros may not. `describe` reports what the workbook
contains so the risk can be checked before writing.
"""

from __future__ import annotations

import re
from pathlib import Path

CELL_RE = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}$")
RANGE_RE = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]{0,6}(:[A-Za-z]{1,3}[1-9][0-9]{0,6})?$")


class SheetError(RuntimeError):
    """The spreadsheet request was refused. Nothing was written."""


def _load(path: Path, read_only: bool = False):
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise SheetError(
            "openpyxl is not installed on this server, so spreadsheets cannot be read"
        ) from exc

    if not path.is_file():
        raise SheetError(f"no such file: {path.name}")
    if path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise SheetError(f"not a spreadsheet this server can open: {path.name}")
    try:
        return openpyxl.load_workbook(path, read_only=read_only, data_only=False)
    except Exception as exc:  # openpyxl raises a wide range of parse errors
        raise SheetError(f"could not open the workbook: {exc}") from exc


def _sheet(book, name: str):
    if not name:
        return book.worksheets[0]
    if name not in book.sheetnames:
        raise SheetError(
            f"no sheet named {name!r} — this workbook has: {', '.join(book.sheetnames)}"
        )
    return book[name]


def describe(path: Path) -> str:
    """List the sheets, their size, and anything a rewrite could damage."""
    book = _load(path, read_only=True)
    lines = []
    for sheet in book.worksheets:
        lines.append(
            f"{sheet.title}: {sheet.max_row} rows x {sheet.max_column} columns"
        )
    book.close()

    full = _load(path)
    risky = []
    for sheet in full.worksheets:
        if getattr(sheet, "_charts", None):
            risky.append(f"{sheet.title}: {len(sheet._charts)} chart(s)")
        if getattr(sheet, "_images", None):
            risky.append(f"{sheet.title}: {len(sheet._images)} image(s)")
        if getattr(sheet, "_pivots", None):
            risky.append(f"{sheet.title}: {len(sheet._pivots)} pivot table(s)")
    full.close()

    if risky:
        lines.append(
            "warning — saving this workbook may drop: " + "; ".join(risky)
        )
    return "\n".join(lines)


def read_range(path: Path, sheet_name: str, ref: str, max_cells: int = 500) -> str:
    """Return the values of a cell or a rectangular range, one cell per line."""
    text = (ref or "").strip().upper()
    if not RANGE_RE.match(text):
        raise SheetError(f"not a cell or range: {ref!r} (expected A1 or A1:D10)")

    book = _load(path, read_only=True)
    try:
        sheet = _sheet(book, sheet_name)
        cells = sheet[text]
        rows = cells if isinstance(cells, tuple) else ((cells,),)
        if rows and not isinstance(rows[0], tuple):
            rows = (rows,)

        out = []
        for row in rows:
            for cell in row:
                if len(out) >= max_cells:
                    out.append(f"… stopped at {max_cells} cells")
                    return "\n".join(out)
                if cell.value is not None:
                    out.append(f"{cell.coordinate}\t{cell.value}")
        return "\n".join(out) if out else "(all cells in that range are empty)"
    finally:
        book.close()


def _typed(value: str):
    """Store numbers as numbers so the spreadsheet can still calculate with them."""
    text = value.strip()
    if not text:
        return None
    if text.startswith("="):  # a formula stays a formula
        return text
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return value


def set_cells(path: Path, sheet_name: str, values: dict[str, str]) -> str:
    """Write values into named cells and save. Refuses anything that is not a cell."""
    if not values:
        raise SheetError("no cells given")
    cleaned = {}
    for address, value in values.items():
        key = (address or "").strip().upper()
        if not CELL_RE.match(key):
            raise SheetError(f"not a single cell address: {address!r} (expected e.g. Q5)")
        cleaned[key] = value

    book = _load(path)
    try:
        sheet = _sheet(book, sheet_name)
        for key, value in cleaned.items():
            sheet[key] = _typed(value)
        book.save(path)
    finally:
        book.close()

    listed = ", ".join(f"{k}={v!r}" for k, v in sorted(cleaned.items()))
    return f"wrote {len(cleaned)} cell(s) to sheet {sheet.title}: {listed}"


def find(path: Path, sheet_name: str, needle: str, max_hits: int = 50) -> str:
    """Find cells whose text contains `needle` — how you locate a row to edit."""
    if not needle:
        raise SheetError("nothing to search for")
    book = _load(path, read_only=True)
    try:
        sheet = _sheet(book, sheet_name)
        lowered = needle.lower()
        hits = []
        for row in sheet.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                if lowered in str(cell.value).lower():
                    hits.append(f"{cell.coordinate}\t{cell.value}")
                    if len(hits) >= max_hits:
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "no matches"
    finally:
        book.close()
