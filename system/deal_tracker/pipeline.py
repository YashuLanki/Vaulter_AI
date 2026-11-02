"""
The live pipeline half of the deal tracker: deals in motion (an LOI, a
counter, a PSA being negotiated, under contract, closing) with their dates,
price, counterparty and next step (team request, 2026-10-29: "a pipeline with
history").

It starts once from the team's own pipeline workbook (`import_workbook`), then
keeps itself current from the deal papers filed on the drive: each offer has
its own folder (".../Disposition/!OFFERS/<year> <buyer>/"), so one offer
folder is one pipeline deal, and only the NEWEST paper in a folder is read --
a month brings 20 to 60 papers, nearly all PSA drafts, and the newest says
where the deal stands. A long PSA is not read whole: the passages about price,
deposit, due diligence and closing are picked out (`corpus.find`).

Closing statements are handled by tracker.py, which also closes the matching
pipeline deal and moves it into the history. Same rules as there: accounting
files are never opened, and a name that fits two deals is asked about.
"""
from __future__ import annotations

import datetime as _dt
import re

from deal_tracker import tracker as T

STAGES = ["LOI Sent", "LOI Received", "Counter Sent", "PSA Negotiation", "Under Contract",
          "Closing Scheduled", "Closed", "Listed", "Unconfirmed", "Terminated", "Declined"]
ACTIVE = {"LOI Sent", "LOI Received", "Counter Sent", "PSA Negotiation", "Under Contract", "Closing Scheduled"}
DONE = {"Closed", "Terminated", "Declined"}

FIELDS = {  # workbook heading -> record key
    "Type": "type", "Property": "property", "Parcel / Lot": "parcel", "APN": "apn", "City": "city",
    "County": "county", "State": "state", "Acres": "acres", "Seller": "seller", "Buyer": "buyer",
    "Stage": "stage", "Status": "status", "LOI Date": "loi_date", "Counter Date": "counter_date",
    "PSA Date": "psa_date", "DD Expires": "dd_expires", "Close Date": "close_date", "Price ($)": "price",
    "Price Basis": "price_basis", "Deposit ($)": "deposit", "Broker": "broker", "Escrow / Title": "escrow",
    "Next Step": "next_step", "Orig. Acq. Date": "orig_acq_date", "Orig. Cost ($)": "orig_cost",
    "Gross Profit ($)": "gross_profit", "Gross Multiple": "gross_multiple", "Gross IRR": "gross_irr",
}
DATE_FIELDS = {"loi_date", "counter_date", "psa_date", "dd_expires", "close_date", "orig_acq_date"}
NUM_FIELDS = {"acres", "price", "deposit", "orig_cost", "gross_profit", "gross_multiple", "gross_irr"}
TERMS = ["Document / Date", "Price", "Deposit", "DD / Feasibility", "Closing", "Key Condition", "Status"]

_DEAL_PATH = re.compile(r"/(acquisition|disposition|!*resales?|!*offers|!*listings?)/", re.I)
_PAPER = re.compile(r"\bloi\b|letter of intent|\bpsa\b|purchase (and sale )?agreement|\bamend|\bcounter|"
                    r"\bcntr\b|\bterminat|listing agreement|\breinstat|\bassignment\b", re.I)
_CLOSING = re.compile(r"\bss\b|settlement statement|closing (statement|stmt)", re.I)
_YEARISH = re.compile(r"^!*(inactive|active|drafts?|final|archive|\d{4})$", re.I)


# ─── Starting from the team's workbook ────────────────────────────────────────

def _cellv(v, key):
    if v in (None, "", "—", "–", "-"):
        return None
    if key in DATE_FIELDS:
        if isinstance(v, (_dt.datetime, _dt.date)):
            return (v.date() if isinstance(v, _dt.datetime) else v).isoformat()
        return str(v).strip()
    if key in NUM_FIELDS:
        n = T._num(v)
        return n if n is not None else str(v).strip()
    return " ".join(str(v).split())


def import_workbook(xlsx) -> tuple[list[dict], list[str]]:
    """(pipeline deals, open items) from the Deal Detail, Offer Terms and Dashboard sheets."""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    try:
        detail = list(wb["Deal Detail"].iter_rows(values_only=True))
        terms = list(wb["Offer Terms"].iter_rows(values_only=True)) if "Offer Terms" in wb.sheetnames else []
        dash = list(wb["Dashboard"].iter_rows(values_only=True)) if "Dashboard" in wb.sheetnames else []
    finally:
        wb.close()
    head_i = next(i for i, r in enumerate(detail) if r and str(r[0] or "").strip() == "Type")
    head = [str(c or "").strip() for c in detail[head_i]]
    deals = []
    for r in detail[head_i + 1:]:
        if not r or str(r[0] or "").strip() not in ("Acquisition", "Disposition"):
            continue
        d = {"id": f"p{len(deals) + 1:03d}", "terms": {}, "documents": [], "folders": [], "history_id": "",
             "sources": {"start": f"Pipeline workbook ({xlsx.name})"}}
        for i, h in enumerate(head):
            if h in FIELDS and i < len(r):
                d[FIELDS[h]] = _cellv(r[i], FIELDS[h])
        deals.append(d)
    # Offer Terms: one column per deal, named short ("Oak Ridge – Pcl 6").
    if terms:
        ti = next((i for i, r in enumerate(terms) if r and str(r[0] or "").strip() == "Deal"), None)
        if ti is not None:
            names = terms[ti][1:]
            rows = {str(r[0] or "").strip(): r[1:] for r in terms[ti + 1:] if r and r[0]}
            for j, short in enumerate(names):
                if not short:
                    continue
                hit = _match(deals, str(short))
                if hit:
                    hit["terms"] = {t: " ".join(str(rows[t][j]).split()) for t in TERMS
                                    if t in rows and j < len(rows[t]) and rows[t][j] not in (None, "")}
    items, on = [], False
    for r in dash:
        cells = [" ".join(str(c).split()) for c in (r or []) if c not in (None, "")]
        if not cells:
            continue
        if cells[0].upper() == "OPEN ITEMS":
            on = True
            continue
        if on:
            items.append(f"{cells[0]}: {' '.join(cells[1:])}" if len(cells) > 1 else cells[0])
    return deals, items


def _words(s: str) -> frozenset:
    s = re.sub(r"\bpcls?\b", "parcel", str(s or "").lower())
    return T._tokens(s)


def _match(deals: list[dict], name: str) -> dict | None:
    """The one deal a short name means, preferring a live one; None if it is not one."""
    want = _words(name)
    hits = [d for d in deals if want and want <= _words(f"{d.get('property', '')} {d.get('parcel', '')}")]
    live = [d for d in hits if d.get("stage") not in ("Unconfirmed", "Closed", "Terminated", "Declined")]
    hits = live or hits
    return hits[0] if len(hits) == 1 else None


# ─── New papers on the drive ──────────────────────────────────────────────────

def offer_folder(path: str) -> str:
    """The folder that holds one offer: the first named folder below Acquisition/Offers/Resales."""
    parts = path.split("/")[:-1]
    marks = [i for i, p in enumerate(parts) if _DEAL_PATH.search("/" + p + "/")]
    if not marks:
        return "/".join(parts)
    j = marks[-1] + 1   # the deepest: ".../Disposition/!OFFERS/<buyer>" is offered by <buyer>
    while j < len(parts) and _YEARISH.match(parts[j].strip()):
        j += 1
    # A folder that is a kind of paper (LOI, PSA) rather than a buyer: the offer is the one above.
    if j < len(parts) and not re.match(r"^(\d+\.\s*)?(loi|psa|closing docs?|cl docs|consent|drafts?|"
                                       r"correspondence|corres|due diligence|dd|title|survey)\b",
                                       parts[j].strip(), re.I):
        return "/".join(parts[:j + 1])
    return "/".join(parts[:j])


def _filed(name: str) -> str:
    from corpus import describe as D
    return D.name_date(name)


def _rank(name: str) -> tuple:
    n = name.lower().replace("_", " ")
    executed = bool(re.search(r"\bfe\b|\bex\b|executed|\bds\b|signed|final", n))
    partial = bool(re.search(r"\bpgs?\b|pages only|\bredline\b|\brl\b|summary", n))
    return (_filed(name), executed, not partial, "clean" in n, n.endswith(".pdf"), n)


def drive_papers(rec: dict, words: list[str]) -> list[dict]:
    """The newest unread paper in each offer folder, filed since the pipeline's start date."""
    from corpus import index
    con = index._connect()
    if con is None:
        return []
    try:
        rows = con.execute("SELECT path, name FROM files WHERE path LIKE '!PROPERTIES/%'").fetchall()
    finally:
        con.close()
    since = rec.get("pipeline_watch_from", "9999")
    groups: dict[str, list[str]] = {}
    for path, name in rows:
        low = name.lower().replace("_", " ")
        if low.endswith((".msg", ".eml")) or not _DEAL_PATH.search(path) or not _PAPER.search(low):
            continue
        if _CLOSING.search(low) or T.blocked(name, words):
            continue
        filed = _filed(name)
        if not filed or filed < since:
            continue
        groups.setdefault(offer_folder(path), []).append(path)
    out = []
    for folder, files in sorted(groups.items()):
        unread = [f for f in files if ("drive:" + f) not in rec["processed"]]
        if not unread:
            continue
        best = max(unread, key=lambda f: _rank(f.rsplit("/", 1)[-1]))
        out.append({"key": "paper:" + folder, "source": "drive", "side": "pipeline",
                    "property_folder": T._property_folder(best), "offer_folder": folder.rsplit("/", 1)[-1],
                    "document": best, "filed": _filed(best.rsplit("/", 1)[-1]),
                    "others": sorted({f.rsplit("/", 1)[-1] for f in unread if f != best})[-8:],
                    "files": files, "hint": next((d["id"] for d in rec.get("pipeline", [])
                                                  if folder in d.get("folders", [])), "")})
    return out


def mark_read(rec: dict, item: dict, result: str, deal_id: str) -> None:
    """Every paper in the folder up to and including the one read."""
    stamp = {"at": _dt.datetime.now().isoformat(timespec="seconds"), "result": result, "deal": deal_id}
    if item["key"].startswith("inbox:"):
        rec["processed"][item["key"]] = stamp
        return
    upto = item["filed"]
    for f in item["files"]:
        if _filed(f.rsplit("/", 1)[-1]) <= upto:
            rec["processed"]["drive:" + f] = stamp


def read_best(rec: dict, item: dict) -> tuple[str, str]:
    """(text, path read): the chosen paper, or the next-best unread one if it will not open."""
    unread = [f for f in item["files"] if ("drive:" + f) not in rec["processed"]
              and _filed(f.rsplit("/", 1)[-1]) == item["filed"]] if item["source"] == "drive" else []
    order = [item["document"]] + sorted((f for f in unread if f != item["document"]),
                                        key=lambda f: _rank(f.rsplit("/", 1)[-1]), reverse=True)
    err = None
    for path in order:
        try:
            text = T.read_text({**item, "document": path}, max_chars=200_000)
        except Exception as e:  # a PDF that will not open: try the Word copy of the same day
            err = e
            continue
        if len(text.strip()) >= 200:   # a reader that failed returns a one-line note, not text
            return passages_for(text), path
        err = ValueError(text.strip()[:200] or "no text")
    raise err


def passages_for(text: str) -> str:
    """The opening (parties, date) and the passages on price, deposit, due diligence and closing."""
    from corpus import find
    if len(text) <= 9000:
        return text
    hits, n = find.find(text, "purchase price deposit earnest money feasibility due diligence period "
                              "inspection closing date close of escrow buyer seller acres lots", top_n=8)
    # A Word paragraph can run to thousands of characters as one passage; 900 each
    # keeps a 40-page PSA to about 9,000 characters (measured: 141,880 -> 25,840 uncapped).
    clip = lambda s: s if len(s) <= 900 else s[:900] + " [...]"
    return (clip(text[:2500].replace("\\", "")) + f"\n\n[... {n} passages in all; the 8 most about price, "
            "deposit, due diligence and closing follow ...]\n\n"
            + "\n\n".join(f"({p['heading']}) {clip(p['text'].replace(chr(92), ''))}" for p in hits))


# ─── Recording what a paper says ──────────────────────────────────────────────

def find_entry(rec: dict, ident: str, deal_type: str = "") -> list[dict]:
    pool = [d for d in rec.get("pipeline", []) if not deal_type or d.get("type", "").lower() == deal_type.lower()]
    hit = [d for d in pool if d["id"] == ident]
    if hit:
        return hit
    want = _words(ident)
    if not want:
        return []
    out = [d for d in pool
           if want <= _words(" ".join(str(d.get(k) or "") for k in ("property", "parcel", "buyer", "seller")))]
    live = [d for d in out if d.get("stage") not in DONE]
    return live or out


def apply(rec: dict, d: dict, fields: dict, source: str) -> list[str]:
    """Set what the paper states. A blank never erases."""
    said = []
    for k, v in fields.items():
        if k not in set(FIELDS.values()) | {"terms"} or v in (None, "", {}):
            continue
        if k == "stage" and v not in STAGES:
            continue
        if k == "terms":
            d["terms"] = {**d.get("terms", {}), **{t: str(x) for t, x in v.items() if t in TERMS and x}}
            said.append("offer terms")
            continue
        v = T._num(v) if k in NUM_FIELDS and T._num(v) is not None else v
        if d.get(k) != v:
            d[k] = v
            d.setdefault("sources", {})[k] = source
            said.append(k.replace("_", " "))
    d["updated"] = _dt.date.today().isoformat()
    return said


def new_entry(rec: dict, deal_type: str, prop: str, source: str) -> dict:
    n = len(rec.setdefault("pipeline", [])) + 1
    d = {"id": f"p{n:03d}", "type": deal_type.title(), "property": prop.strip(), "terms": {}, "documents": [],
         "folders": [], "history_id": "", "sources": {"start": source}}
    rec["pipeline"].append(d)
    return d


def close_from_statement(rec: dict, history_deal: dict, kind: str, date: str, price, other_party: str) -> str:
    """A closing statement closes the matching live pipeline deal. Returns what it did, or ''."""
    want = "Disposition" if kind == "sale" else "Acquisition"
    hits = []
    for n in [history_deal["name"], *history_deal.get("aliases", [])]:
        hits = [d for d in find_entry(rec, n, want) if d.get("stage") not in DONE]
        if hits:
            break
    if other_party and len(hits) > 1:
        party = _words(other_party)
        hits = [d for d in hits if party & _words(d.get("buyer" if kind == "sale" else "seller", ""))] or hits
    if len(hits) != 1:
        return ""
    d = hits[0]
    d.update(stage="Closed", close_date=date or d.get("close_date"), history_id=history_deal["id"], next_step="Closed")
    if price is not None and not isinstance(d.get("price"), float):
        d["price"] = T._num(price)
    return f"pipeline deal {d['property']} marked Closed"


# ─── Worked out for the page ──────────────────────────────────────────────────

def key_date(d: dict, today: _dt.date) -> tuple[str, str]:
    """(label, ISO date) of the date that matters next, else the latest that happened."""
    ahead = [(lab, _iso(d.get(k))) for lab, k in (("Close", "close_date"), ("DD ends", "dd_expires"))
             if _iso(d.get(k)) and _iso(d.get(k)) >= today.isoformat()]
    if ahead and d.get("stage") not in DONE:
        return min(ahead, key=lambda x: x[1])
    past = [(lab, _iso(d.get(k))) for lab, k in (("Closed" if d.get("stage") == "Closed" else "Close", "close_date"),
                                                ("PSA", "psa_date"), ("Counter", "counter_date"), ("LOI", "loi_date"))
            if _iso(d.get(k))]
    return max(past, key=lambda x: x[1]) if past else ("", "")


def _iso(v) -> str:
    if not v:
        return ""
    s = str(v)[:10]
    return s if re.match(r"\d{4}-\d{2}-\d{2}$", s) else ""
