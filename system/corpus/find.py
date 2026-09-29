"""
Find the passages inside ONE document that answer a question.

Built 2026-09-29. Reading a document into a conversation sends all of it:
measured on 25 real library documents, a median of about 4,400 tokens and up
to 33,000 each, and it stays in the conversation for every later message.
Most questions asked of a document are "what does it say about X" -- the
price, a deadline, who pays for title -- so this reads the file on the
person's OWN machine, exactly as read_document does, and sends back only the
passages most likely to answer, each with its page.

What it deliberately is not:
  * Not a search across the library. It opens the one file Claude already
    chose, so nothing new is downloaded beyond what read_document would, and
    no permission boundary is crossed.
  * Not a verdict. It ranks words (the same BM25 ranking and synonym list as
    summary_search.py), and the summary search taught that no score can tell
    a right passage from a wrong one. So it returns candidates, and every
    reply tells Claude to read the whole document if none plainly answers.
  * Not a way around the scan budget. A scanned document is searched only as
    far as scanning got, and the reply says so; a page never scanned was
    never searched, which is different from "not in the document".

Tables are left out of the search because pdfplumber's page text already
contains them; they were being sent twice.
"""
from __future__ import annotations

import re

_PAGE = re.compile(r"^\[Page (\d+)( - OCR)?\]\s*$")
_TABLE = re.compile(r"^\[Table on Page (\d+)\]\s*$")
_SHEET = re.compile(r"^\[Sheet: (.+)\]\s*$")
_MAX = 750


def passages(text: str) -> list[dict]:
    """
    Split extracted text into passages of at most ~750 characters, each
    labelled with where it sits: "page 12", "page 3 (scanned)", "sheet
    'Budget'", "reviewer comment", or a Word heading. A spreadsheet chunk
    repeats its sheet's first row, so a number never arrives without the
    column headings that say what it is.
    """
    out, buf = [], []
    # "document", not a page: a Word file has no pages, and a passage labelled
    # "page 1" there was cited as page 1 in testing -- a page that does not exist.
    where, heading, sheet_head = "document (no page numbers)", "", ""
    in_table = False
    in_comments = text.lstrip().startswith("[") and "REVIEWER COMMENT" in text[:300]

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        if len(body) < 20:
            return
        label = where + (f" › {heading}" if heading else "")
        if sheet_head and not body.startswith(sheet_head):
            body = sheet_head + "\n" + body
        out.append({"heading": label, "text": body})

    for line in text.split("\n"):
        m = _PAGE.match(line)
        if m:
            flush(); in_table = False; in_comments = False; sheet_head = ""
            where = f"page {m.group(1)}" + (" (scanned)" if m.group(2) else "")
            continue
        if _TABLE.match(line):
            flush(); in_table = True
            continue
        m = _SHEET.match(line)
        if m:
            flush(); in_table = False; in_comments = False
            where, heading, sheet_head = f"sheet '{m.group(1)}'", "", ""
            continue
        if in_table:
            continue
        if in_comments:
            if line.startswith("[") and "REVIEWER COMMENT" in line:
                where = "reviewer comment"
                continue
        h = re.match(r"^#{1,4}\s+(.*)$", line)
        if h:
            flush(); heading = h.group(1).strip()[:80]
            continue
        if where.startswith("sheet") and not sheet_head and line.strip():
            sheet_head = line.strip()[:300]
            continue
        if not line.strip():
            if sum(len(x) for x in buf) > 200:
                flush()
            continue
        buf.append(line)
        if sum(len(x) for x in buf) > _MAX:
            flush()
    flush()
    return out


def find(text: str, question: str, top_n: int = 8) -> tuple[list[dict], int]:
    """(best passages, how many passages the document had)."""
    import summary_search as ss
    ps = passages(text)
    for p in ps:
        p["terms"] = ss._terms(p["heading"] + " " + p["text"])
        p["head_terms"] = set(ss._terms(p["heading"])) - {"page", "sheet", "scanned"}
    ranked = ss.rank(question, ps, ps)
    # A reviewer's sticky note is short, and BM25 favours short passages, so
    # "price looks high?" outranked the purchase price clause itself (caught by
    # check_portfolio_comparison.py §10). Notes still come back, labelled; they
    # just rank behind the document's own words.
    ranked = sorted(((sc * (0.6 if p["heading"] == "reviewer comment" else 1.0), cov, p)
                     for sc, cov, p in ranked), key=lambda x: -x[0])
    return [p for _, _, p in ranked[:top_n]], len(ps)
