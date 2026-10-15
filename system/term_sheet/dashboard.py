"""
The mini term sheet as a page: one self-contained web page beside each saved
workbook, rebuilt from the workbook on every save (team request, 2026-10-12:
"a dashboard rather than the current format", with Excel one click away).

The workbook stays the record. The page is a VIEW of it: nothing here is
worked out, every figure and date is read from a cell, and anything derived
(days counted from today, a verdict tally) says so on the page. One source,
so the page can never disagree with the Excel file, and "export to Excel" is
the file that already exists.

It holds real deal data, so it is a file in the team folder, opened from
OneDrive like the screening report, and is never published as a web page.
No external resources: it must open on a laptop with no network.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path

from term_sheet import sheet as _sheet

TEMPLATE = Path(__file__).with_name("dashboard_template.html")

_MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)\s*(million|m\b)?", re.I)
_ACRES = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:gross |net |usable |total )?acres?", re.I)
_DATE = re.compile(
    r"(?<![\d/])(?:(January|February|March|April|May|June|July|August|September|October|November|"
    r"December)\s+(\d{1,2}),?\s+(\d{4})|(\d{1,2})\s+(January|February|March|April|May|June|July|"
    r"August|September|October|November|December),?\s+(\d{4})|(\d{1,2})/(\d{1,2})/(\d{4})|"
    r"(\d{4})-(\d{2})-(\d{2}))(?![\d/])")
_MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                      "August", "September", "October", "November", "December"], 1)}
# What a row's text means, from its first words. Matched longest first.
_STATES = [("needs input", "gap"), ("not provided", "open"), ("not completed", "open"),
           ("not received", "open"), ("not drafted", "open"), ("not approved", "open"),
           ("not established", "open"), ("not determined", "open"), ("not estimated", "open"),
           ("not current", "open"), ("not confirmed", "unknown"), ("not verified", "unknown"),
           ("not applicable", "na"), ("no amendment", "unknown"), ("only the signed", "filled"),
           ("not on file", "unknown"), ("none", "unknown"), ("yes", "ok"), ("low risk", "ok"),
           ("confirmed", "ok"), ("complete", "ok"), ("signed copy", "ok"), ("executed", "ok"),
           ("recorded", "ok")]
_FLAGS = re.compile(r"does not match|must be reconciled|needs reconcil|disagree|not legible|"
                    r"has (now )?passed|unresolved|cannot be relied|conflict", re.I)


def _state(text: str, kind: str) -> str:
    low = (text or "").strip().lower()
    if not low or set(low) <= {"_"}:
        return "blank"
    for prefix, state in _STATES:
        if low.startswith(prefix):
            return state
    return "filled"


def _dates_in(text: str) -> list[_dt.date]:
    out = []
    for m in _DATE.finditer(text or ""):
        try:
            if m.group(1):
                out.append(_dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2))))
            elif m.group(5):
                out.append(_dt.date(int(m.group(6)), _MONTHS[m.group(5)], int(m.group(4))))
            elif m.group(7):
                out.append(_dt.date(int(m.group(9)), int(m.group(7)), int(m.group(8))))
            else:
                out.append(_dt.date(int(m.group(10)), int(m.group(11)), int(m.group(12))))
        except ValueError:
            continue
    return out


def _money(text: str, largest: bool = False) -> str:
    """The first dollar figure in the text, or the largest (a total sits beside its per-acre rate)."""
    ms = list(_MONEY.finditer(text or ""))
    if not ms:
        return ""
    # The latest settlement figure, where the row names one, is the price the
    # rules say leads; else the largest, since a total sits beside its rate.
    after = [m for m in ms if re.search(r"settlement", (text or "")[:m.start()], re.I)]
    if largest and after:
        ms = after[-1:]
    elif largest:
        def val(m):
            try:
                return float(m.group(1).replace(",", "")) * (1_000_000 if m.group(2) else 1)
            except ValueError:
                return 0
        ms.sort(key=val, reverse=True)
    m = ms[0]
    return "$" + m.group(1).rstrip(",.") + (" million" if m.group(2) else "")


_PER_ACRE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*(?:per|an|/|a)\s*(?:usable |net |gross )?acre", re.I)


def _per_acre(text: str) -> str:
    """Only a figure the row itself calls per acre; a price quoted in passing is not one."""
    m = _PER_ACRE.search(text or "")
    return ("$" + m.group(1)) if m else ""


def _acres(text: str) -> str:
    m = _ACRES.search(text or "")
    return (m.group(1) + " acres") if m else ""


def _logo(path: Path) -> str:
    """The workbook's own logo (the one picture in it), as data the page can embed."""
    import base64
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            media = [n for n in z.namelist() if n.startswith("xl/media/")]
            if not media:
                return ""
            name = media[0]
            kind = "png" if name.lower().endswith(".png") else "jpeg"
            data = z.read(name)
            # The picture file carries a two-pixel dark frame on its edges that
            # Excel's picture style hides and a browser shows (owner, 2026-10-13:
            # "get rid of the black border line"). Trimmed here, never in the file.
            try:
                import io
                from PIL import Image
                im = Image.open(io.BytesIO(data))
                w, h = im.size
                if w > 20 and h > 20:
                    im = im.crop((4, 4, w - 4, h - 4))
                    buf = io.BytesIO()
                    im.save(buf, format="PNG")
                    data, kind = buf.getvalue(), "png"
            except Exception:
                pass
            return f"data:image/{kind};base64," + base64.b64encode(data).decode("ascii")
    except (OSError, zipfile.BadZipFile):
        return ""


def read_workbook(path: Path) -> dict:
    """Every answerable row of every tab, with what it says."""
    import openpyxl
    rows = _sheet.template_rows()
    wb = openpyxl.load_workbook(path, read_only=True)
    # The page is named after the property's folder, as the file is (owner,
    # 2026-10-13: "the title should be simplified"), and carries the workbook's
    # own logo so it looks like the sheet.
    out = {"tabs": {}, "title": "", "file": path.name, "folder": path.parent.name, "logo": _logo(path)}
    try:
        ov = wb["Overview"]
        for r in range(1, 8):
            v = ov.cell(r, 1).value
            if v and str(v).strip().upper().endswith("MINI TERM SHEET"):
                out["title"] = str(v).strip()
                break
        by_tab: dict[str, list] = {}
        for tab in _sheet.TABS:
            if tab not in wb.sheetnames:
                continue
            ws = wb[tab]
            cells = {}
            for row in ws.iter_rows(min_row=1, max_col=3):
                for c in row:
                    if c.value is not None:
                        cells[(c.row, c.column)] = str(c.value).strip()
            for r in rows:
                if r["tab"] != tab:
                    continue
                if r["kind"] == "todo":
                    by_tab.setdefault(tab, []).append({"label": r["label"], "section": r["section"],
                                                       "kind": "todo", "value": "", "state": "todo"})
                    continue
                val = cells.get((r["row"], 2), "")
                src = cells.get((r["row"], 3), "") if r.get("has_source") else ""
                by_tab.setdefault(tab, []).append({
                    "label": r["label"], "section": r["section"], "kind": r["kind"], "key": r["key"],
                    "value": "" if _sheet._is_blank_line(val) else val, "source": src,
                    "state": _state(val, r["kind"]), "flag": bool(_FLAGS.search(val or ""))})
        out["tabs"] = by_tab
    finally:
        wb.close()
    return out


def _find(rows: list, label: str) -> str:
    for r in rows:
        if r["label"].lower().startswith(label.lower()):
            return r["value"]
    return ""


def build_payload(book: dict, today: _dt.date | None = None) -> dict:
    """What the page shows, all read from the workbook; derived bits say so."""
    today = today or _dt.date.today()
    ov = book["tabs"].get("Overview", [])
    legal = book["tabs"].get("Legal", [])
    dd = book["tabs"].get("Due Diligence", [])
    pf = book["tabs"].get("Proforma", [])
    name = _find(ov, "Property / Project Name") or book["title"].replace(" MINI TERM SHEET", "").title()
    price_text = _find(ov, "Purchase Price Total")
    low = price_text.lower()
    status, since = "", ""
    years = re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", price_text)
    if low.startswith("current transaction price not provided") or "acquired" in low or "purchased" in low:
        status, since = "Owned", (years[0] if years else "")
    elif ("under contract" in low or "contract price" in low or "being acquired" in low
          or ("agreement" in low and ("buyer" in low or "purchaser" in low))):
        status = "Under contract"

    figures = [
        {"label": "Purchase price", "value": _money(price_text, largest=True) or "not given", "note": "Overview, Purchase Price Total"},
        {"label": "Price per acre", "value": _per_acre(_find(ov, "Price per Acre")) or "not given", "note": "Overview, Price per Acre"},
        {"label": "Parcel size", "value": _acres(_find(ov, "Parcel Size")) or "not given", "note": "Overview, Parcel Size"},
    ]
    closing = _find(legal, "Closing Date")
    cd = _dates_in(closing)
    figures.append({"label": "Closing date" if status != "Owned" else "Purchase closed",
                    "value": cd[0].strftime("%B %d, %Y").replace(" 0", " ") if cd else "not given",
                    "note": "Legal, Closing Date"})

    glance = [(l, _find(ov, l)) for l in ("Owner", "Seller Contact", "Location", "County", "APN(s)",
                                           "Current Zoning", "Lot Size", "Price per Lot")]
    glance = [{"label": l, "value": v} for l, v in glance if v]

    # Dates on the Legal tab, each with the row it sits in: deadlines ahead
    # for a deal in progress, history for an owned property.
    events = []
    for r in legal:
        if r["state"] in ("blank", "unknown", "gap"):
            continue
        for d in _dates_in(r["value"]):
            events.append({"date": d.isoformat(), "label": r["label"], "section": r["section"],
                           "text": r["value"][:160], "ahead": d >= today,
                           "days": (d - today).days})
    seen, uniq = set(), []
    for e in sorted(events, key=lambda e: (e["date"], e["label"])):
        k = (e["date"], e["label"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    events = uniq
    # The dates to lead with: what is coming for a deal in progress; for an
    # owned property, how the purchase ran (the first pick led with the
    # newest title commitment, which is not a key date of anything).
    ahead = [e for e in events if e["ahead"]][:6]
    purchase = [e for e in events if re.search(r"purchase|closing", e["section"], re.I)]
    key_dates = ahead or purchase[:6] or events[-4:]

    # What needs a person: the yellow rows, every "Not provided", a flagged
    # figure, and the diligence items the report says are not done.
    attention = []
    for r in ov:
        if r["state"] == "gap":
            attention.append({"what": r["label"] + " is still needed", "where": "Overview", "kind": "gap"})
    for tab, rs in (("Legal", legal), ("Due Diligence", dd), ("Proforma", pf)):
        for r in rs:
            if r.get("flag"):
                hit = next((x.strip() for x in re.split(r"(?<=[.!?])\s+", r["value"]) if _FLAGS.search(x)), r["value"])
                attention.append({"what": r["label"] + ": " + hit[:220], "where": tab, "kind": "flag"})
            elif r["state"] == "open" and tab != "Legal":
                attention.append({"what": r["label"] + ": " + r["value"][:160], "where": tab, "kind": "open"})
            elif r["state"] == "open" and r["value"].lower().startswith("not provided"):
                attention.append({"what": r["label"] + " not provided", "where": tab, "kind": "open"})

    counts = {"ok": 0, "open": 0, "unknown": 0, "filled": 0, "na": 0}
    for r in dd:
        counts[r["state"] if r["state"] in counts else "filled"] += 1
    legal_filled = sum(1 for r in legal if r["state"] not in ("blank", "unknown"))
    pf_filled = sum(1 for r in pf if r["state"] not in ("blank", "unknown"))

    return {
        "name": book.get("folder") or name, "title": book["title"], "file": book["file"],
        "logo": book.get("logo", ""), "status": status, "since": since,
        "built": today.isoformat(), "figures": figures, "glance": glance, "events": events,
        "key_dates": key_dates, "key_dates_title": "Next dates" if ahead else "How the purchase ran",
        "attention": attention,
        "progress": [
            {"tab": "Due Diligence", "done": sum(1 for r in dd if r["state"] != "blank"), "of": len(dd),
             "note": f"{counts['ok']} confirmed, {counts['open']} open, {counts['unknown']} not confirmed"},
            {"tab": "Legal", "done": legal_filled, "of": len(legal), "note": "steps with a fact on them"},
            {"tab": "Proforma", "done": pf_filled, "of": len(pf), "note": "rows with a figure or note"},
        ],
        "tabs": book["tabs"],
    }


def write_dashboard(xlsx: Path, today: _dt.date | None = None) -> Path:
    """Build the page beside the workbook and return its path."""
    book = read_workbook(xlsx)
    payload = build_payload(book, today)
    html = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/", "const DATA=" + data + ";")
    html = html.replace("__TITLE__", _esc(payload["name"]) + " mini term sheet")
    out = xlsx.with_suffix(".html")
    tmp = out.with_name("~tmp_" + out.name)
    tmp.write_text(html, encoding="utf-8")
    try:
        tmp.replace(out)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise _sheet.SheetOpenError(f"{out.name} is open and cannot be replaced; close it and save again.") from None
    return out


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
