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
import re
from pathlib import Path

import config
from deal_tracker import excel as X
from deal_tracker import pipeline as P
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


# The Great Recession, as the old spreadsheet's "Owned During Recession" column dates it.
RECESSION = (_dt.date(2007, 12, 1), _dt.date(2009, 6, 30))
RETURN_YEARS = (5, 10, 15, None)   # None = all time


def _recession(bought: _dt.date | None, last: _dt.date | None, status: str, marked: bool) -> bool | None:
    """
    Held at any point in the recession, from the dates. Where they cannot tell (a
    loss with no sale date, say), the old spreadsheet's own mark; else None.
    """
    if bought and not (status == "Fully exited" and not last):
        return bought <= RECESSION[1] and (status != "Fully exited" or last >= RECESSION[0])
    if bought and bought > RECESSION[1]:
        return False
    return True if marked else None


def returns(deals: list[dict], today: _dt.date) -> list[dict]:
    """
    Deals fully exited in the last 5, 10 and 15 years, and all time, worked out the
    old spreadsheet's way: cash multiple = distributions / contributions, and IRR =
    each deal's IRR weighted by its share of the profit.
    """
    out = []
    for n in RETURN_YEARS:
        since = today.replace(year=today.year - n, day=min(today.day, 28)) if n else None
        sold = [d for d in deals if d["status"] == "Fully exited" and d["last_sale"]
                and d["contributions"] is not None and d["distributions"] is not None
                and (since is None or _dt.date.fromisoformat(d["last_sale"]) >= since)]
        put, back = sum(d["contributions"] for d in sold), sum(d["distributions"] for d in sold)
        withirr = [d for d in sold if d["irr"] is not None and d["profit"] is not None]
        prof = sum(d["profit"] for d in withirr)
        out.append({"years": n, "deals": len(sold), "contributions": round(put), "distributions": round(back),
                    "multiple": round(back / put, 2) if put else None,
                    "irr": round(sum(d["profit"] * d["irr"] for d in withirr) / prof, 4) if prof > 0 else None,
                    "irr_deals": len(withirr)})
    return out


def deal_row(d: dict, today: _dt.date) -> dict:
    status = "Fully exited" if T.status(d) == "Lost" else T.status(d)
    bought, approx = _date(d.get("acquired"))
    ends = [s["date"] for s in d["sales"] if s.get("date")] + ([d["last_sale"]] if d.get("last_sale") else [])
    last, last_approx = _date(max(ends)) if ends else (None, False)
    end = last if status == "Fully exited" else today
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
        "sold_in_parts": d.get("sold_in_parts", False), "hold": hold, "hold_to_today": status != "Fully exited",
        "recession": _recession(bought, last, status, d.get("recession_marked", False)),
        "sales": d["sales"], "excluded": d.get("excluded", ""), "notes": d.get("notes", []),
        "sources": d.get("sources", {}), "lost": d.get("lost", False),
        # The land, from the Smartsheet project dashboards (smartsheet_dashboards.py).
        "location": ", ".join(x for x in (d.get("city"), d.get("county") and d["county"] + " County", d.get("state")) if x),
        **{k: d.get(k, "") for k in ("cross_streets", "apn", "zoning", "density", "lot_sizes")},
    }


def pipeline_row(d: dict, today: _dt.date) -> dict:
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


def web_address(path: Path) -> str:
    r"""
    The file's SharePoint address, from OneDrive's own map of synced folders
    (the same records config._library_from_onedrive_records reads). Excel opens
    a SharePoint address at once; handed a file on this computer it first asks
    whether to trust the folder. "" when the file is not in a synced library.
    """
    import sys
    from urllib.parse import quote
    if sys.platform != "win32":
        return ""
    best = None
    try:
        import winreg
        key = r"Software\SyncEngines\Providers\OneDrive"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        url = str(winreg.QueryValueEx(sub, "UrlNamespace")[0])
                        mount = Path(str(winreg.QueryValueEx(sub, "MountPoint")[0])).resolve()
                    rel = Path(path).resolve().relative_to(mount)
                except (OSError, ValueError):
                    continue
                # The deepest mount wins: the account root also contains the library.
                if url.lower().startswith("https://") and (best is None or len(mount.parts) > best[0]):
                    best = (len(mount.parts), url, rel)
    except Exception:
        return ""
    if not best:
        return ""
    _, url, rel = best
    return quote(url.rstrip("/"), safe=":/%") + "/" + quote(rel.as_posix())


def add_sites(rows: list[dict], name_key: str) -> None:
    """
    A Google Earth link on each row whose site is in the team's hand-checked
    locations (property_coordinates.csv, the table proximity uses). Never a
    location worked out from a name: that was measured wrong 5 times in 8. A
    location matching more than one deal, or a deal matching two locations, gets
    no link. "rough" marks a location known only to the town.
    """
    try:
        from pipeline import property_coordinates as PC
        coords = PC.load_coordinates(config.DATA_DIR)
    except Exception:
        return
    pool = [{"name": r.get(name_key) or "", "aliases": r.get("aliases") or [], "i": i} for i, r in enumerate(rows)]
    # The site's name as a PHRASE in the deal's name: "Cedar Draw" must not match
    # "Mission Flat - NEC of Elm & Cedar Draw Rd" unless the words sit together.
    phrase = lambda s: " " + " ".join(re.findall(r"[a-z]+|\d+", str(s).lower().replace("&", " and "))) + " "
    part_of = lambda key, r: any(phrase(key) in phrase(n) for n in [r["name"], *r["aliases"]])
    found: dict[int, list] = {}
    for key, c in coords.items():
        want = T._tokens(key)
        hits = T.matches({"deals": pool}, key, pool)
        if len(want) >= 2:   # an exact match stops matches() early; the site's other parts count too
            hits += [r for r in pool if r not in hits and part_of(key, r)]
        # Several rows share one site only when each is a part of it: its name holds the
        # site's whole name ("<site> – Lot 2", "<site> – Lot 4"). Otherwise one row or none.
        if len(hits) > 1 and not all(part_of(key, h) or want in T._deal_tokens(h) for h in hits):
            continue
        for h in hits:
            shared = max(len(want & t) for t in T._deal_tokens(h))
            found.setdefault(h["i"], []).append((shared, c["lat"], c["lon"], c["precision"] == "city"))
    for i, sites in found.items():
        # A row matching two sites takes the one sharing more of its name; a tie gets no link.
        sites.sort(reverse=True)
        if len(sites) == 1 or sites[0][0] > sites[1][0] or sites[0][1:] == sites[1][1:]:
            _, lat, lon, rough = sites[0]
            rows[i]["site"] = {"url": f"https://earth.google.com/web/search/{lat},{lon}", "rough": rough}


def build_payload(rec: dict, today: _dt.date | None = None, book: Path | None = None) -> dict:
    today = today or _dt.date.today()
    deals = [deal_row(d, today) for d in rec["deals"]]
    pipeline = [pipeline_row(d, today) for d in rec.get("pipeline") or []]
    add_sites(deals, "name")
    add_sites(pipeline, "property")
    return {"built": today.isoformat(), "book_url": web_address(book) if book else "",
            "built_at": _dt.datetime.now().strftime("%B %d, %Y at %I:%M %p").replace(" 0", " "),
            "imported_from": rec.get("imported_from", ""), "watch_from": rec.get("watch_from", ""),
            "deals": deals, "returns": returns(deals, today), "inbox_warning": T.inbox_warning(rec, today), "attention": rec.get("attention", []), "pipeline": pipeline,
            "updates": rec.get("updates", []), "stage_words": P.PLAIN_STAGE,
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
    if folder == config.DEAL_TRACKER_DIR:
        config.DEAL_DROP_DIR.mkdir(exist_ok=True)   # where a person hands in a paper
    book = folder / BOOK_NAME
    if X.pull_edits(rec, book):
        T.save(rec)
    payload = build_payload(rec, today, book)
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
