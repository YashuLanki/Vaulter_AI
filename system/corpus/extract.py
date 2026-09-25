"""
corpus/extract.py
-----------------
Turn one document from the firm's library into plain text for Claude.

Moved from the old `ingestion/extractor.py`, which fed a chunker and an
embedder. Nothing chunks or embeds any more -- the text goes straight back to
the conversation -- so two things changed:

  * The Excel path no longer stamps `page_count = 9999`. That sentinel existed
    purely to trigger an 8000-character chunk tier in `config.CHUNK_TIERS` so
    CoStar rows wouldn't be split mid-row. There is no chunker to signal.
  * `.docx` is supported (via mammoth). The library is full of Word documents;
    the old extractor skipped them because only the email-attachment path
    handled Word, and that path is gone.

Reading a file here HYDRATES it -- OneDrive downloads the bytes on first
access. That is the whole reason `corpus.index.search` works on filenames
instead of content: search stays free, and only the specific document someone
chose to open costs a download.

Known gap: `.msg` (archived Outlook messages) is not supported. The library
has a lot of them and they likely carry real correspondence history, but
reading them needs another dependency (`extract-msg`). Flagged, not silently
skipped -- `read_document` says so explicitly when asked for one.
"""

import itertools
import logging
import re
from datetime import datetime
from pathlib import Path

import pdfplumber
import os
import shutil
import tempfile
import time as _t
from contextlib import contextmanager
import pytesseract
from pdf2image import convert_from_path

from config import TESSERACT_PATH, POPPLER_PATH
from corpus.index import is_online_only, resolve_in_corpus

pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

log = logging.getLogger("vaulter.corpus.extract")

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xls", ".csv", ".txt", ".md"}

# Read but explained rather than silently returning nothing.
_KNOWN_UNSUPPORTED = {
    ".msg": "an archived Outlook message (needs the extract-msg package, not installed)",
    ".eml": "an archived email (no reader installed)",
    ".pptx": "a PowerPoint deck (no reader installed)",
    ".dwg": "a CAD drawing",
    ".shp": "a GIS shapefile",
}


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def read_document(rel_path: str, max_chars: int = 200_000) -> tuple[str, dict]:
    """
    Read one document out of the library and return (text, metadata).

    `rel_path` is corpus-relative and is resolved through the scope guard, so
    this cannot be pointed at anything outside the document library.

    Args:
        rel_path:  path relative to CORPUS_DIR, as returned by corpus.search
        max_chars: truncate beyond this, with a marker. Guards against handing
                   back a 400-page appraisal in one blob.
    """
    path = resolve_in_corpus(rel_path)
    if not path.is_file():
        raise FileNotFoundError(f"No such document in the library: {rel_path}")

    ext = path.suffix.lower()
    metadata = {
        "path": rel_path,
        "filename": path.name,
        "file_type": ext,
        "size_bytes": path.stat().st_size,
        "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "was_online_only": is_online_only(path),
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "page_count": 0,
        "has_tables": False,
        "ocr_used": False,
        "comment_count": 0,
        "truncated": False,
    }

    if ext not in SUPPORTED_EXTENSIONS:
        described = _KNOWN_UNSUPPORTED.get(ext, f"an unsupported file type ({ext})")
        return f"[Cannot read {path.name}: it is {described}.]", metadata

    if ext == ".pdf":
        text, metadata = _extract_pdf(path, metadata)
    elif ext == ".docx":
        text, metadata = _extract_docx(path, metadata)
    elif ext in (".xlsx", ".xls"):
        text, metadata = _extract_excel(path, metadata)
    elif ext == ".csv":
        text, metadata = _extract_csv(path, metadata)
    else:
        text, metadata = _extract_txt(path, metadata)

    if len(text) > max_chars:
        metadata["truncated"] = True
        metadata["full_length"] = len(text)
        text = (
            text[:max_chars]
            + f"\n\n[Truncated at {max_chars:,} of {len(text):,} characters. "
              f"Ask for a specific section or page range to see more.]"
        )
    return text, metadata


# ─── PDF ──────────────────────────────────────────────────────────────────────

# How much scanning ONE document may cost. This file previously had neither
# limit, against this project's own hard rule ("never scan a PDF without a
# timeout", earned when an OCR fallback reached 6.5 GB before being killed).
#
# Measured 2026-09-01: one scanned page costs about 11s at 300 dpi. So a
# 200-page scanned drawing set -- this library holds several, up to 37 MB --
# would spend over half an hour inside a single tool call, which Claude Desktop
# abandons long before. The read then looks like a crash and says nothing useful.
#
# When either limit is reached the returned text SAYS SO. A truncated read that
# admits it is useful; one that stays quiet is the confident partial answer this
# project distrusts everywhere else.
# The budget can only be checked BETWEEN pages -- a page already being scanned
# cannot be interrupted -- so the real ceiling is the budget plus one page.
# Measured 2026-09-01 on a 37 MB, 42-page scanned plan set: a 90s budget
# produced a 141s read, because a single architectural sheet at 300 dpi is a
# very large image. 40s keeps the common overshoot inside what a tool call will
# wait for; a pathological single page can still exceed it, and that is stated
# here rather than pretended away.
#
# Ten pages is not a guess either: on a scanned plan set the informative pages
# are the cover sheet, the index and the first few sheets. Reading further in
# costs minutes and usually adds line-work with no lettering.
_OCR_MAX_PAGES = 10
_OCR_TIME_BUDGET_SECONDS = 40


@contextmanager
def _poppler_readable(path: Path):
    r"""
    A path poppler can actually open.

    Poppler is a native program and cannot open a path over Windows' classic
    260-character limit -- and 86,228 documents here, 17.4% of the library, are
    over it, because a synced SharePoint library nests deeply. Python opens them
    fine, so pdfplumber works and only the OCR step failed: a scanned document
    in a deep folder was simply unreadable, with an I/O error naming no cause.

    The extended-length "\?" prefix does NOT help (measured -- poppler still
    refuses), so the file is copied to a short temporary path and removed
    afterwards. Only when the path really is too long; copying every file would
    add real cost for the 82% that do not need it.
    """
    if len(str(path)) < 250:
        yield path
        return
    tmp = None
    try:
        fd, tmp_name = tempfile.mkstemp(suffix=path.suffix or ".pdf")
        os.close(fd)
        tmp = Path(tmp_name)
        shutil.copy2(path, tmp)
        log.info("  Path is %d chars -- copied to a short path for OCR" % len(str(path)))
        yield tmp
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# --- PDF reviewer comments ---------------------------------------------------
#
# WHY THIS EXISTS, measured 2026-09-21. A PDF keeps reviewer comments in a
# separate ANNOTATION layer that page.extract_text() does not touch, so for as
# long as this module has existed those comments were unreadable. Not a
# cosmetic gap: bringing nine property summaries up to date in one afternoon,
# THREE of the findings existed ONLY as sticky notes -- including the sole
# piece of evidence anywhere that one deal had fallen through, which happened
# to be that property's biggest open question. 954 PDFs in this library carry
# "comments" in their own filename, so the gap is wide as well as deep.
#
# Three things about the shape of this are deliberate:
#
# * A COMMENT IS NOT PAGE TEXT, and is never mixed into it. A sticky note is
#   one person's informal remark, often a question or a proposed edit, and
#   quoting it as though the document said it is exactly the confident-wrong
#   answer this project removes everywhere else. Real example from the run
#   that prompted this: beside a "$530 per square foot" line a reviewer had
#   written only "$5.30?". That is a doubt, not a correction, and must read as
#   neither the document's own figure nor a fact.
# * EVERY COMMENT CARRIES ITS DATE, because the date is what makes it usable.
#   On one property the comments turned out to PREDATE the summary being
#   checked, so the right answer was "nothing new here" -- reachable only
#   because the dates were visible.
# * THEY GO FIRST, not last. read_document truncates at max_chars from the END,
#   so comments appended after a long document's text would be cut off on
#   precisely the documents where a reader most needs to know they exist.

_MAX_COMMENTS = 40

# A /Link is not a comment, and a /Popup is only the on-screen bubble belonging
# to another annotation -- both would be noise. Requiring real contents rules
# them out too, without maintaining a subtype allowlist a new PDF writer could
# defeat.
_NOT_COMMENT_SUBTYPES = {"link", "popup"}


def _pdf_date(raw) -> str:
    """
    A PDF date (D:20260908171335-07'00') as plain YYYY-MM-DD, or "" when it
    cannot be read. Returns "" rather than guessing: an unreadable date must
    never become a wrong one, the same rule the staleness checks follow.
    """
    try:
        text = raw.decode("latin-1", "replace") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if text.startswith("D:"):
            text = text[2:]
        if len(text) < 8 or not text[:8].isdigit():
            return ""
        return text[0:4] + "-" + text[4:6] + "-" + text[6:8]
    except Exception:
        return ""


def _pdf_comments(pdf) -> tuple[list, int]:
    """
    Reviewer comments out of the PDF's annotation layer, as (lines, count).

    Never raises. A document whose annotations cannot be parsed must still be
    readable -- a partial answer that says so beats a crash, which under MCP is
    indistinguishable from a hang.
    """
    lines, count, capped = [], 0, False
    for page in pdf.pages:
        try:
            annots = page.annots or []
        except Exception:
            continue
        for annot in annots:
            try:
                body = " ".join((annot.get("contents") or "").split())
                if not body:
                    continue
                data = annot.get("data") or {}
                raw_sub = data.get("Subtype", "")
                if isinstance(raw_sub, bytes):
                    raw_sub = raw_sub.decode("latin-1", "replace")
                sub = "".join(c for c in str(raw_sub) if c.isalpha()).lower()
                if sub in _NOT_COMMENT_SUBTYPES:
                    continue
                if count >= _MAX_COMMENTS:
                    capped = True
                    continue
                when = _pdf_date(data.get("CreationDate")) or _pdf_date(data.get("M"))
                who = (annot.get("title") or "").strip()
                stamp = " on ".join(x for x in (who, when) if x)
                if not stamp:
                    stamp = "author and date not recorded"
                lines.append("  - [page %s] (%s) %s"
                             % (annot.get("page_number"), stamp, body))
                count += 1
            except Exception:
                continue
    if capped:
        lines.append("  - [Only the first %d comments are listed; this document has"
                     " more. Everything above is real.]" % _MAX_COMMENTS)
    return lines, count


def _extract_pdf(path: Path, metadata: dict) -> tuple[str, dict]:
    """
    Extract each page with pdfplumber. Any individual page that yields no
    text layer (e.g. a scanned image page mixed into an otherwise digital PDF)
    falls back to Tesseract OCR for that page only, so a mostly-digital PDF
    with a few scanned pages doesn't silently drop those pages -- only a
    whole-document OCR fallback would have caught an all-scanned PDF, missing
    the mixed case.
    """
    full_text = []

    with pdfplumber.open(path) as pdf:
        metadata["page_count"] = len(pdf.pages)

        if pdf.metadata:
            metadata["pdf_title"]  = pdf.metadata.get("Title", "") or ""
            metadata["pdf_author"] = pdf.metadata.get("Author", "") or ""

        _ocr_pages = 0
        _ocr_started = _t.perf_counter()
        for page_num, page in enumerate(pdf.pages, start=1):
            text = page.extract_text()

            if text and text.strip():
                full_text.append(f"[Page {page_num}]\n{text.strip()}")
            else:
                over_pages = _ocr_pages >= _OCR_MAX_PAGES
                over_time = (_t.perf_counter() - _ocr_started) > _OCR_TIME_BUDGET_SECONDS
                if over_pages or over_time:
                    # Stop, and SAY so. Silence would hand back a document
                    # that looks complete and is not.
                    if not metadata.get("ocr_truncated"):
                        metadata["ocr_truncated"] = True
                        full_text.append(
                            "[Scanning stopped at page %d of %s. This document has more"
                            " scanned pages than one read will process (%d pages or %ds)."
                            " Everything above is real; pages from here on were NOT read."
                            " Ask for a specific page range if you need further in.]"
                            % (page_num, metadata["page_count"], _OCR_MAX_PAGES,
                               _OCR_TIME_BUDGET_SECONDS))
                    continue

                log.info(f"  Page {page_num} has no text layer — running OCR...")
                # Render only this one page, not the whole document -- a
                # mostly-digital PDF with one scanned/blank page would
                # otherwise pay to rasterize every page at 300 DPI just to
                # OCR the one that needs it.
                with _poppler_readable(path) as _ocr_path:
                    page_images = convert_from_path(
                        str(_ocr_path), dpi=300, poppler_path=POPPLER_PATH,
                        first_page=page_num, last_page=page_num,
                    )
                metadata["ocr_used"] = True
                _ocr_pages += 1

                if page_images:
                    ocr_text = pytesseract.image_to_string(page_images[0], lang="eng")
                    if ocr_text.strip():
                        full_text.append(f"[Page {page_num} - OCR]\n{ocr_text.strip()}")
                    else:
                        # A drawing with no lettering OCR can read. Say it,
                        # rather than returning a silently empty page.
                        full_text.append(
                            "[Page %d is an image with no text OCR could read --"
                            " typically a drawing, map or plan. It was scanned, not"
                            " skipped.]" % page_num)

            tables = page.extract_tables()
            if tables:
                metadata["has_tables"] = True
                for table in tables:
                    table_text = _table_to_text(table, page_num)
                    if table_text:
                        full_text.append(table_text)

        # Read this while the document is still open, and never let it break
        # the read -- the page text is the thing that must always come back.
        try:
            comment_lines, comment_count = _pdf_comments(pdf)
        except Exception as exc:
            log.warning("  Could not read this PDF's comments: %s" % exc)
            comment_lines, comment_count = [], 0

    metadata["comment_count"] = comment_count
    body = "\n\n".join(full_text)
    if not comment_lines:
        return body, metadata

    header = (
        "[%d REVIEWER COMMENT(S) are attached to this PDF. These are notes people"
        " added to the file -- they are NOT text the document itself says. Treat"
        " each as that person's remark on the date shown: a question or a proposed"
        " change, not an established fact, and never a correction to the document"
        " unless a signed or recorded document agrees. The document's own pages"
        " follow below.]" % comment_count
    )
    return header + "\n" + "\n".join(comment_lines) + "\n\n" + body, metadata


def _table_to_text(table: list, page_num: int) -> str:
    """Convert a pdfplumber table (list of lists) into readable plain text."""
    if not table:
        return ""
    lines = [f"[Table on Page {page_num}]"]
    for row in table:
        cleaned = [str(cell).strip() if cell else "" for cell in row]
        lines.append(" | ".join(cleaned))
    return "\n".join(lines)


# ─── Word ─────────────────────────────────────────────────────────────────────

def _extract_docx(path: Path, metadata: dict) -> tuple[str, dict]:
    """Extract a Word document as markdown, which keeps headings and tables."""
    import mammoth

    try:
        with open(path, "rb") as f:
            # Pictures are dropped, not embedded. mammoth's default writes every
            # image into the text as base64 -- measured 2026-09-24 on a 17 MB
            # environmental study: the first 400,000 characters returned were
            # encoded picture data with not one word of the document in them, so a
            # capped read saw nothing but gibberish and a full read would have put
            # megabytes of noise into a conversation. A placeholder says a picture
            # was there, which is all the text layer can honestly say about it.
            result = mammoth.convert_to_markdown(
                f, convert_image=mammoth.images.img_element(lambda image: {"src": ""}))
        text = re.sub(r"!\[[^\]]*\]\(\s*\)", "[picture]", result.value)
        metadata["page_count"] = 1
        metadata["has_tables"] = "|" in text
        metadata["pictures_dropped"] = text.count("[picture]")
        return text, metadata
    except Exception as e:
        log.error(f"  [ERROR] Failed to extract Word file: {e}")
        return f"[Could not read {path.name}: {e}]", metadata


# ─── Excel ────────────────────────────────────────────────────────────────────

def _extract_excel(path: Path, metadata: dict) -> tuple[str, dict]:
    """
    Extract all sheets and cells from an Excel file (.xlsx or .xls).
    Each sheet is converted to readable plain text with rows and columns.
    """
    import openpyxl

    full_text = []
    metadata["has_tables"] = True

    try:
        wb_formulas = None
        if path.suffix.lower() == ".xlsx":
            wb = openpyxl.load_workbook(path, data_only=True)
            # data_only=True returns None for any formula cell that was never
            # recalculated/saved by Excel (e.g. a workbook generated
            # programmatically and never opened in Excel) -- a row made up
            # entirely of such cells looks completely empty and would be
            # silently skipped below, even though it has real (just uncached)
            # data. Load a second, formula-preserving copy so we can tell
            # "genuinely blank row" apart from "all-uncalculated-formula row"
            # and fall back to showing the formula text itself rather than
            # losing the row entirely.
            wb_formulas = openpyxl.load_workbook(path, data_only=False)
        else:
            import xlrd
            xls_wb = xlrd.open_workbook(str(path))
            wb = _convert_xls_to_openpyxl(xls_wb)

        metadata["page_count"] = len(wb.sheetnames)

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_lines = [f"[Sheet: {sheet_name}]"]

            formula_rows = wb_formulas[sheet_name].iter_rows(values_only=True) if wb_formulas else iter(())
            for row, formula_row in itertools.zip_longest(ws.iter_rows(values_only=True), formula_rows, fillvalue=()):
                if all(cell is None for cell in row):
                    if any(isinstance(c, str) and c.startswith("=") for c in formula_row):
                        # Not actually empty -- every cell is an uncalculated
                        # formula. Show the formula text since we can't
                        # evaluate it ourselves.
                        cleaned = [str(c) if c is not None else "" for c in formula_row]
                        sheet_lines.append(" | ".join(cleaned))
                    continue
                cleaned = [str(cell).strip() if cell is not None else "" for cell in row]
                sheet_lines.append(" | ".join(cleaned))

            if len(sheet_lines) > 1:
                full_text.append("\n".join(sheet_lines))

        log.info(f"  Extracted {metadata['page_count']} sheet(s) from Excel file")

    except Exception as e:
        log.error(f"  [ERROR] Failed to extract Excel file: {e}")
        return f"[Could not read {path.name}: {e}]", metadata

    return "\n\n".join(full_text), metadata


def _convert_xls_to_openpyxl(xls_wb):
    """Convert an xlrd workbook to openpyxl format for uniform processing."""
    import openpyxl
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    for sheet_idx in range(xls_wb.nsheets):
        xls_sheet = xls_wb.sheet_by_index(sheet_idx)
        ws = wb.create_sheet(title=xls_sheet.name)
        for row in range(xls_sheet.nrows):
            for col in range(xls_sheet.ncols):
                ws.cell(row=row + 1, column=col + 1, value=xls_sheet.cell_value(row, col))

    return wb


# ─── CSV / plain text ─────────────────────────────────────────────────────────

def _extract_csv(path: Path, metadata: dict) -> tuple[str, dict]:
    """Extract a CSV as readable plain text via pandas."""
    import pandas as pd

    metadata["has_tables"] = True

    try:
        try:
            df = pd.read_csv(path, encoding="utf-8")
        except UnicodeDecodeError:
            df = pd.read_csv(path, encoding="latin-1")

        metadata["page_count"] = 1

        lines = [
            f"[CSV File: {path.name}]",
            f"Columns: {' | '.join(str(c) for c in df.columns)}",
            f"Rows: {len(df)}",
            "",
        ]
        for _, row in df.iterrows():
            lines.append(" | ".join(str(v) for v in row.values))

        log.info(f"  Extracted {len(df)} rows from CSV file")
        return "\n".join(lines), metadata

    except Exception as e:
        log.error(f"  [ERROR] Failed to extract CSV file: {e}")
        return f"[Could not read {path.name}: {e}]", metadata


def _extract_txt(path: Path, metadata: dict) -> tuple[str, dict]:
    """Read a plain text or markdown file directly."""
    try:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")

        metadata["page_count"] = 1
        log.info(f"  Extracted {len(text):,} characters from text file")
        return text, metadata

    except Exception as e:
        log.error(f"  [ERROR] Failed to read text file: {e}")
        return f"[Could not read {path.name}: {e}]", metadata
