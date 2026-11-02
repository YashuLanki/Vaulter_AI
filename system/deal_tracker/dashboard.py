"""
The deal tracker as a page and an Excel copy, rebuilt from the record each
time the tracker is opened or a document is recorded.

Like the mini term sheet page, the page is a VIEW: every figure is read from
the record. The few things worked out here (status, hold period, profit as
returned minus put in, days out, the 1- and 5-year views) are labelled as
worked out on the page. The Excel copy (excel.py) is two-way: edits saved in it
are read back into the record before anything is rebuilt.

It holds real deal figures, so both are files in the team folder, opened from
OneDrive, and never published as a web page. No external resources: the page
must open on a laptop with no network.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import config
from deal_tracker import excel as X
from deal_tracker import tracker as T

TEMPLATE = Path(__file__).with_name("dashboard_template.html")
PAGE_NAME = "Deal Tracker.html"
BOOK_NAME = "Deal Tracker.xlsx"


def _date(v: str | None) -> tuple[_dt.date | None, bool]:
    """(date, approximate). A bare year counts as mid-year and says so."""
    if not v:
        return None, False
    try:
        if len(v) == 4:
            return _dt.date(int(v), 7, 1), True
        return _dt.date.fromisoformat(v[:10]), False
    except ValueError:
        return None, False


def deal_row(d: dict, today: _dt.date) -> dict:
    status = T.status(d)
    bought, approx = _date(d.get("acquired"))
    ends = [s["date"] for s in d["sales"] if s.get("date")] + ([d["last_sale"]] if d.get("last_sale") else [])
    last, last_approx = _date(max(ends)) if ends else (None, False)
    end = last if status == "Fully exited" else (None if status == "Lost" else today)
    hold = round((end - bought).days / 365.25, 1) if bought and end and end >= bought else None
    put, back = d.get("contributions"), d.get("distributions")
    profit = round(back - put) if put is not None and back is not None and not d.get("lost") else None
    return {
        "id": d["id"], "name": d["name"], "aliases": d.get("aliases", []), "state": d.get("state", ""),
        "type": d.get("type", ""), "size": d.get("size", ""), "acres": d.get("acres"), "lots": d.get("lots"),
        "status": status, "stage": d.get("stage", ""),
        "acquired": bought.isoformat() if bought else "", "acquired_approx": approx,
        "purchase_price": d.get("purchase_price"), "debt": d.get("acquisition_debt"),
        "contributions": put, "distributions": back, "profit": profit,
        "multiple": round(back / put, 2) if put and back is not None and not d.get("lost") else None,
        "irr": d.get("project_irr"), "last_sale": last.isoformat() if last else "", "last_sale_approx": last_approx,
        "sold_in_parts": d.get("sold_in_parts", False), "hold": hold, "hold_to_today": status not in ("Fully exited", "Lost"),
        "sales": d["sales"], "excluded": d.get("excluded", ""), "notes": d.get("notes", []),
        "sources": d.get("sources", {}), "lost": d.get("lost", False),
    }


def pipeline_row(d: dict, today: _dt.date) -> dict:
    from deal_tracker import pipeline as P
    label, when = P.key_date(d, today)
    days = (_dt.date.fromisoformat(when) - today).days if when else None
    loc = ", ".join(x for x in (d.get("city"), d.get("state")) if x)
    return {**{k: v for k, v in d.items() if k != "sources"}, "location": loc,
            "counterparty": d.get("buyer") if d.get("type") == "Disposition" else d.get("seller"),
            "key_label": label, "key_date": when, "deadline": label in ("Close", "DD ends"),
            "days_out": days if d.get("stage") in P.ACTIVE else None, "sources": d.get("sources", {})}


def _file_list_date() -> str:
    try:
        from corpus import index
        age = index.index_age()
        return age[1].date().isoformat() if age else ""
    except Exception:
        return ""


def build_payload(rec: dict, today: _dt.date | None = None) -> dict:
    today = today or _dt.date.today()
    return {"built": today.isoformat(),
            "built_at": _dt.datetime.now().strftime("%B %d, %Y at %I:%M %p").replace(" 0", " "),
            "imported_from": rec.get("imported_from", ""), "watch_from": rec.get("watch_from", ""),
            "deals": [deal_row(d, today) for d in rec["deals"]], "attention": rec.get("attention", []),
            "pipeline": [pipeline_row(d, today) for d in rec.get("pipeline") or []],
            "pipeline_from": rec.get("pipeline_from", ""), "pipeline_watch_from": rec.get("pipeline_watch_from", ""),
            "open_items": rec.get("open_items", []), "file_list": _file_list_date(),
            "read": sum(1 for p in rec.get("processed", {}).values()
                        if p.get("result") not in ("not a purchase or sale", "not a deal paper"))}


def write(rec: dict, folder: Path | None = None, today: _dt.date | None = None) -> Path:
    """
    Build the page and the Excel copy in the tracker folder; return the page.
    Edits saved in the Excel copy are read back first, so a rebuild can never
    throw them away.
    """
    folder = folder or config.DEAL_TRACKER_DIR
    folder.mkdir(parents=True, exist_ok=True)
    book = folder / BOOK_NAME
    if X.pull_edits(rec, book):
        T.save(rec)
    payload = build_payload(rec, today)
    html = TEMPLATE.read_text(encoding="utf-8")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/", "const DATA=" + data + ";")
    page = folder / PAGE_NAME
    _swap(page, lambda tmp: tmp.write_text(html, encoding="utf-8"))
    snap = {}
    try:
        _swap(book, lambda tmp: snap.update(X.write(payload, tmp)))
        rec["excel_snapshot"], rec["excel_mtime"] = snap, book.stat().st_mtime
        T.save(rec)
    except PermissionError:
        pass  # open in Excel right now; its saved edits were read above, the copy catches up next time
    return page


def _swap(target: Path, writer) -> None:
    tmp = target.with_name("~tmp_" + target.name)
    writer(tmp)
    try:
        tmp.replace(target)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise
