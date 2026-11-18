"""
The email inbox's two workbooks (owner, 2026-11-17).

Ava's Power Automate flows save every attachment, code-named, in `Attachments/`
and add a row for it to `Attachments Log.xlsx`; an email or calendar event that
names a deal date gets a row in `Date Updates.xlsx` (tabs Calendar and Email).
The columns are the owner's, in plain words; each sheet also carries a hidden ID
column, which is what the tracker and the status flow key on.

This module only reads and creates those files. Nothing here decides what is a
deal paper: `tracker` screens and picks. The Attachments Log holds real email
subjects, so it is read here for file names only and never handed to Claude.
"""
from __future__ import annotations

import re
from pathlib import Path

ATTACH_DIR = "Attachments"
ATTACH_LOG = "Attachments Log.xlsx"
DATES_BOOK = "Date Updates.xlsx"

COLUMNS = {
    "Attachments": ["Received", "From", "Subject", "File", "Deal", "Status", "ID"],
    "Calendar": ["Received", "From", "Event", "When", "Details", "Words found", "Deal", "Status", "ID"],
    "Email": ["Received", "From", "Subject", "Details", "Words found", "Deal", "Status", "ID"],
}
STATUSES = ["New", "Done", "Not a deal", "Skipped", "Needs a look"]
_BOOKS = {ATTACH_LOG: ["Attachments"], DATES_BOOK: ["Calendar", "Email"]}
_WIDTHS = {"Received": 20, "From": 24, "Subject": 40, "Event": 40, "File": 36, "When": 30, "Details": 60,
           "Words found": 24, "Deal": 24, "Status": 14, "ID": 10}


class InboxUnreadable(Exception):
    """The workbook is there but would not open (locked, half-synced, damaged)."""


def make_workbooks(inbox: Path) -> list[Path]:
    """Create whichever workbook is missing, each sheet a table the flows can add rows to."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / ATTACH_DIR).mkdir(exist_ok=True)
    made = []
    for book, sheets in _BOOKS.items():
        path = inbox / book
        if path.exists():
            continue
        wb = Workbook()
        wb.remove(wb.active)
        for name in sheets:
            ws = wb.create_sheet(name)
            cols = COLUMNS[name]
            ws.append(cols)
            for i, c in enumerate(cols, 1):
                ws.cell(1, i).font = Font(bold=True)
                ws.column_dimensions[get_column_letter(i)].width = _WIDTHS.get(c, 18)
            ws.column_dimensions[get_column_letter(len(cols))].hidden = True
            ws.freeze_panes = "A2"
            # A table needs at least one row under its headings; the flows add to it.
            ws.append([None] * len(cols))
            t = Table(displayName=name, ref=f"A1:{get_column_letter(len(cols))}2")
            t.tableStyleInfo = TableStyleInfo(name="TableStyleLight9", showRowStripes=True)
            ws.add_table(t)
        wb.save(path)
        made.append(path)
    return made


def rows(inbox: Path, sheet: str) -> list[dict]:
    """Every row of one sheet as heading -> text; rows with no ID are left out."""
    book = next((b for b, s in _BOOKS.items() if sheet in s), None)
    path = inbox / book if book else None
    if not path or not path.is_file():
        return []
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True, data_only=False)
    except Exception as e:
        raise InboxUnreadable(f"{book} could not be opened ({type(e).__name__})") from e
    try:
        if sheet not in wb.sheetnames:
            return []
        it = wb[sheet].iter_rows(values_only=True)
        head = [str(h).strip() if h is not None else "" for h in next(it, [])]
        out = []
        for r in it:
            row = {h: ("" if v is None else str(v).strip()) for h, v in zip(head, r) if h}
            if row.get("ID"):
                out.append(row)
        return out
    finally:
        wb.close()


def missing(inbox: Path) -> list[str]:
    """Each workbook or tab the flows write to that is not there, in plain words
    ("Date Updates", "Date Updates, Email tab"). Raises InboxUnreadable if a workbook will not open."""
    from openpyxl import load_workbook
    out = []
    for book, sheets in _BOOKS.items():
        path = inbox / book
        name = book.rsplit(".", 1)[0]
        if not path.is_file():
            out.append(name)
            continue
        try:
            wb = load_workbook(path, read_only=True)
        except Exception as e:
            raise InboxUnreadable(f"{book} could not be opened ({type(e).__name__})") from e
        try:
            out += [f"{name}, {s} tab" for s in sheets if s not in wb.sheetnames]
        finally:
            wb.close()
    return out


_LAST_QUOTED = re.compile(r'"((?:[^"]|"")*)"\s*\)\s*$')


def _shown(cell: str) -> str:
    """What a File cell shows: the second argument of =HYPERLINK(url, name), or the value itself."""
    if cell.upper().startswith("=HYPERLINK("):
        m = _LAST_QUOTED.search(cell)
        return m.group(1).replace('""', '"') if m else ""
    return cell


def attachment_names(inbox: Path) -> dict[str, str]:
    """Code name (the row ID) -> the attachment's real file name."""
    return {r["ID"]: _shown(r.get("File", "")) for r in rows(inbox, "Attachments") if _shown(r.get("File", ""))}


def date_rows(inbox: Path) -> list[dict]:
    """Calendar and Email rows, each with its tab."""
    return [{**r, "tab": tab} for tab in ("Calendar", "Email") for r in rows(inbox, tab)]


def row_text(row: dict) -> str:
    """The plain lines Claude reads for one row."""
    if row.get("tab") == "Calendar":
        lines = [f"Calendar event from {row.get('From') or 'unknown'}, saved {row.get('Received', '')}.",
                 f"Event: {row.get('Event', '')}", f"When: {row.get('When', '')}"]
    else:
        lines = [f"Email from {row.get('From') or 'unknown'}, received {row.get('Received', '')}.",
                 f"Subject: {row.get('Subject', '')}"]
    lines.append(f"Details: {row.get('Details', '')}")
    return "\n".join(lines)
