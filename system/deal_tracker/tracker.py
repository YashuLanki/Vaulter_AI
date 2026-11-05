"""
The firm's acquisition and disposition record, kept current from what already
gets updated (team request, 2026-10-29).

The old tracker was a spreadsheet someone had to remember to update, and
nobody did: its newest purchase is from 2021 and its newest sale from 2024. So
this module never asks a person to type a deal in. It starts from that
spreadsheet once (`import_tracker`), then each time the tracker is opened it
adds what has changed since, from four places:

  * the Smartsheet Project Master (new deals, stage, a struck-through name
    meaning sold), read through `portfolio.py`;
  * the property summaries' data cards (purchase price and year, a sale);
  * closing statements filed on the firm's drive, found by NAME in the file
    list -- nothing is opened until Claude reads the one chosen per closing;
  * a folder of saved email attachments, only on the machine whose own
    `.env` names it (`config.DEAL_INBOX_DIR`). Nothing here reads email.

The live pipeline (offers in motion) is pipeline.py; the Excel copy excel.py.

Three rules, each asked for by the owner:

  * **Accounting files are never opened.** Any file whose name carries a word
    on the "do not open" list is skipped before anything reads it, and is not
    listed or recorded anywhere -- hidden completely, not shown as skipped.
    The list is a plain file beside the record so it can grow without a code
    change.
  * **Documents fill, the tracker's history stands.** A figure from a document
    fills an empty field; where it disagrees with a figure already there, the
    old one is kept and the disagreement is listed for a person. A signed
    statement does outrank a figure taken from a summary's card.
  * **Never guess which deal.** A name that matches more than one deal is not
    linked; it is listed for a person, the same rule the summaries follow.

The record holds real deal figures, so it lives in the team folder and never
in this repository.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import re
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
RECORD_FILE = config.DEAL_TRACKER_RECORD_FILE
# Beside the record, not in the Deal Tracker folder: that folder holds only the
# page and its Excel copy (owner, 2026-10-29). Ask Claude to add a word.
DO_NOT_OPEN_FILE = RECORD_FILE.with_name("do not open - words.txt")

# Seeded on first use; the file is what is read.
DEFAULT_DO_NOT_OPEN = [
    "accounting", "k-1", "k1", "capital account", "bank statement", "bank stmt",
    "general ledger", "ledger", "trial balance", "p&l", "profit and loss",
    "income statement", "balance sheet", "cash flow", "tax return", "1099", "w-9", "w9",
    "payroll", "invoice", "quickbooks", "wire instructions", "confidential",
]

_STOP = {"rmg", "llc", "lp", "lllp", "llp", "inc", "the", "of", "and", "investors", "investments",
         "investment", "holdings", "holding", "properties", "property", "partners", "ltd", "co"}

# How a closing's folder is named: everything below the closing itself is
# drafts, archive and the statement folder, and is grouped as one closing.
_CLOSING_SUBFOLDERS = {"ss", "est ss", "archive", "settlement statements", "settlement statement",
                       "closing statements", "closing statement", "final", "drafts", "executed"}
_SS_FOLDER = re.compile(r"/(est )?ss/|/settlement statements?/|/closing statements?/", re.I)
_SS_NAME = re.compile(r"\bsettlement statement\b|\bclosing (statement|stmt)\b|\bclosing (buyer|seller)\b|"
                      r"\bss\b|\bhud\b|\balta statement\b", re.I)
_NOT_A_CLOSING = re.compile(r"/loan/|\brefi\b|\brefinanc|\bsig(nature)? ?(pg|page)s?\b|\bdeed of trust\b|"
                            r"\bltr\b|\bletter\b|\bmemo\b", re.I)
_SALE_PATH = re.compile(r"/(disposition|!*resales?|!*offers)/", re.I)
_BUY_PATH = re.compile(r"/acquisition/", re.I)


# ─── The record ───────────────────────────────────────────────────────────────

def load() -> dict | None:
    """The record, or None if there is none yet or it is the wrong shape."""
    try:
        rec = json.loads(RECORD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or not isinstance(rec.get("deals"), list):
        log.warning("Deal tracker record at %s is not the expected shape; ignoring it.", RECORD_FILE)
        return None
    rec.setdefault("processed", {})
    rec.setdefault("attention", [])
    return rec


def save(rec: dict) -> bool:
    """Write the record if it changed. True if it was written."""
    text = json.dumps(rec, indent=1, ensure_ascii=False, default=str)
    try:
        if RECORD_FILE.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    RECORD_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECORD_FILE.with_name("~tmp_" + RECORD_FILE.name)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(RECORD_FILE)
    return True


def _note(rec: dict, text: str) -> None:
    """Something for a person, once."""
    if text not in rec["attention"]:
        rec["attention"].append(text)


MAX_UPDATES = 300


def log_update(rec: dict, deal: str, what: str, why: str = "", source: str = "") -> None:
    """
    One line for "What changed", newest first (owner, 2026-11-03: keep the
    company told what is going on and why). `why` is only ever what a paper or
    a person said; blank when nobody did, never a guess.
    """
    if what:
        rec.setdefault("updates", []).insert(0, {"date": _dt.date.today().isoformat(), "deal": deal,
                                                 "what": what, "why": (why or "").strip(), "source": source})
        del rec["updates"][MAX_UPDATES:]


def plain_money(v) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)


def plain_date(v) -> str:
    try:
        return _dt.date.fromisoformat(str(v)[:10]).strftime("%b %d, %Y").replace(" 0", " ")
    except ValueError:
        return str(v)


# ─── Importing the old spreadsheet ────────────────────────────────────────────

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("$", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _iso(v):
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    return None


def parse_size(text) -> tuple[float | None, int | None]:
    """(acres, lots) as written in a size cell such as '29.6 acres (114 FLs)'."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text), None
    s = str(text or "").replace(",", "")
    a = re.search(r"(\d+(?:\.\d+)?)\s*(?:gross |net )?acres?", s, re.I)
    lots = re.findall(r"(\d+)\s*(?:[a-z&]+\s)?(?:lots|fls|pils|pil)\b", s, re.I)
    return (float(a.group(1)) if a else None), (sum(int(n) for n in lots) if lots else None)


_TRACKER_COLUMNS = {
    "number": "deal #", "name": "property name", "state": "state", "type": "type", "size": "size",
    "approach": "status", "debt": "acquisition debt", "first_in": "date of initial contribution",
    "last_sold": "last parcel date sold", "contributions": "contributions",
    "distributions": "distributions/return", "irr": "project irr", "excluded": "excluded from additional analysis",
    "recession": "owned during recession - dec. 2007 to june 2009",
}
_END_LABELS = {"sold deals", "current deals", "total lifetime deals", "stats for website"}


def import_tracker(xlsx: Path) -> dict:
    """
    A new record from the old tracker's FIRST sheet. Rows above its "TOTAL
    SOLD PROPERTIES" line are sold deals, rows below it current ones; the
    totals and website figures at the bottom are not deals and are left out.
    The old status codes are kept in the record but not shown: what they mean
    is not yet confirmed.
    """
    import openpyxl
    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    head = [str(c or "").strip().lower() for c in rows[0]]
    col = {key: head.index(title) for key, title in _TRACKER_COLUMNS.items() if title in head}
    missing = [t for k, t in _TRACKER_COLUMNS.items() if k not in col and k in ("name", "first_in", "contributions")]
    if missing:
        raise ValueError(f"The first sheet has no column headed {', '.join(missing)}; is this the tracker?")

    def cell(row, key):
        i = col.get(key)
        return row[i] if i is not None and i < len(row) else None

    deals, section, newest = [], "sold", None
    for row in rows[1:]:
        name = cell(row, "name")
        if not isinstance(name, str) or not name.strip():
            continue
        name = " ".join(name.split())
        low = name.lower()
        if low.startswith("total sold"):
            section = "current"
            continue
        if low in _END_LABELS:
            break
        acres, lots = parse_size(cell(row, "size"))
        dist_raw, last_raw = cell(row, "distributions"), cell(row, "last_sold")
        d = {
            "id": f"t{len(deals) + 1:04d}", "name": name, "aliases": [],
            "number": cell(row, "number") if isinstance(cell(row, "number"), int) else None,
            "state": str(cell(row, "state") or "").strip().rstrip(","),
            "type": str(cell(row, "type") or "").strip(),
            "size": " ".join(str(cell(row, "size") or "").split()),
            "acres": acres, "lots": lots,
            "approach": str(cell(row, "approach") or "").strip(),
            "section": section,
            "acquired": _iso(cell(row, "first_in")),
            "purchase_price": None,
            "acquisition_debt": _num(cell(row, "debt")),
            "contributions": _num(cell(row, "contributions")),
            "distributions": _num(dist_raw),
            "lost": isinstance(dist_raw, str) and dist_raw.strip().upper() == "LOST",
            "project_irr": _num(cell(row, "irr")),
            "last_sale": _iso(last_raw),
            "sold_in_parts": isinstance(last_raw, str) and last_raw.strip().lower() == "var",
            "excluded": str(cell(row, "excluded") or "").strip(),
            # The old sheet's hand-made "x"; the dates decide first (dashboard._recession).
            "recession_marked": str(cell(row, "recession") or "").strip().lower() == "x",
            "sales": [], "stage": "", "smartsheet_sold": False, "notes": [],
            "sources": {"history": f"Old tracker ({xlsx.name})"},
        }
        if isinstance(dist_raw, str) and not d["lost"] and dist_raw.strip():
            d["notes"].append(f"Tracker says: {dist_raw.strip()}")
        for k in ("acquired", "last_sale"):
            if d[k] and (newest is None or d[k] > newest):
                newest = d[k]
        deals.append(d)
    watch = (_dt.date.fromisoformat(newest) + _dt.timedelta(days=1)).isoformat() if newest else "2000-01-01"
    return {"format_version": FORMAT_VERSION, "imported_from": xlsx.name,
            "imported_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "watch_from": watch, "deals": deals, "processed": {}, "attention": []}


def add_pipeline(rec: dict, xlsx: Path) -> None:
    """Load the live pipeline from the team's pipeline workbook; papers filed from its date on update it."""
    from deal_tracker import pipeline
    deals, items = pipeline.import_workbook(xlsx)
    m = re.search(r"(20\d\d)(\d\d)(\d\d)", xlsx.name)
    rec["pipeline"] = deals
    rec["open_items"] = items
    rec["pipeline_from"] = xlsx.name
    rec["pipeline_watch_from"] = (f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m
                                  else _dt.date.fromtimestamp(xlsx.stat().st_mtime).isoformat())


# ─── Matching names across sources ────────────────────────────────────────────

def _tokens(name: str) -> frozenset:
    s = str(name or "").lower().replace("&", " and ")
    s = re.sub(r"\(.*?\)", lambda m: " " + m.group(0)[1:-1] + " ", s)
    return frozenset(t for t in re.findall(r"[a-z]+|\d+", s) if t not in _STOP)


def _deal_tokens(d: dict) -> list[frozenset]:
    return [_tokens(n) for n in [d["name"], *d.get("aliases", [])]]


def matches(rec: dict, name: str, among: list | None = None) -> list[dict]:
    """
    Deals a name could mean. An exact name or recorded alias wins outright;
    otherwise a deal matches when every word of the shorter name is in the
    longer one ("Oak Ridge 40" and "Oak Ridge 40 Investors, LLC"). Numbers
    count as words, so Oak Ridge 40 never matches Oak Ridge 80.
    """
    pool = among if among is not None else rec["deals"]
    want = _tokens(name)
    if not want:
        return []
    exact = [d for d in pool if want in _deal_tokens(d)]
    if exact:
        return exact
    out = []
    for d in pool:
        for t in _deal_tokens(d):
            if not t:
                continue
            small, big = (want, t) if len(want) <= len(t) else (t, want)
            if len(small) >= 2 and small <= big:
                out.append(d)
                break
    return out


def likely_same(rec: dict, name: str) -> dict | None:
    """
    A SUGGESTION only, never applied: the one current tracker deal not yet tied
    to Smartsheet that shares a number, or two words, with this name
    ("Pine Flat 12" and "Holdings VII, LLC (Pine Flat 12)").
    """
    want = _tokens(name)
    best, tie = None, False
    for d in rec["deals"]:
        if d["section"] != "current" or d.get("stage"):
            continue
        shared = set().union(*[want & t for t in _deal_tokens(d)])
        if len(shared) < 2:
            continue
        if best is None or len(shared) > best[0]:
            best, tie = (len(shared), d), False
        elif len(shared) == best[0]:
            tie = True
    return best[1] if best and not tie else None


def merge(rec: dict, keep: dict, drop: dict) -> None:
    """
    Join a deal added from Smartsheet into the tracker deal it turned out to be.
    The tracker's record is kept, under the Smartsheet name: that is the project
    name people use ("Oak Ridge 40"), and the old spreadsheet's company
    name becomes another name for it.
    """
    if drop.get("stage") and keep["section"] != "new":
        keep["name"], drop["name"] = drop["name"], keep["name"]
    keep["aliases"] = sorted({*keep["aliases"], drop["name"], *drop["aliases"]} - {keep["name"]})
    for k in ("stage", "smartsheet_sold"):
        keep[k] = drop.get(k) or keep.get(k)
    for k, v in drop.items():
        if k in ("id", "name", "aliases", "section", "sources", "notes", "sales") or v in (None, "", [], False):
            continue
        if keep.get(k) in (None, "", []):
            keep[k] = v
    keep["sales"] = sorted(keep["sales"] + [s for s in drop["sales"] if s not in keep["sales"]],
                           key=lambda s: s["date"])
    keep["sources"] = {**drop["sources"], **keep["sources"]}
    keep["notes"] += [n for n in drop["notes"] if n not in keep["notes"]]
    rec["deals"] = [d for d in rec["deals"] if d is not drop]
    for p in rec["processed"].values():
        if p.get("deal") == drop["id"]:
            p["deal"] = keep["id"]
    rec["attention"] = [a for a in rec["attention"]
                        if f'"{drop["name"]}"' not in a and f'"{keep["name"]}"' not in a]


def find_deal(rec: dict, ident: str) -> list[dict]:
    """By record id, else by name."""
    hit = [d for d in rec["deals"] if d["id"] == ident]
    return hit or matches(rec, ident)


def link_name(rec: dict, name: str, deal: dict) -> None:
    if name.strip() and name.strip() != deal["name"] and name.strip() not in deal["aliases"]:
        deal["aliases"].append(name.strip())
    rec["attention"] = [a for a in rec["attention"] if f'"{name.strip()}"' not in a]


def new_deal(rec: dict, name: str, source: str, **fields) -> dict:
    n = sum(1 for d in rec["deals"] if d["id"].startswith("n")) + 1
    d = {"id": f"n{n:04d}", "name": name.strip(), "aliases": [], "number": None, "state": "",
         "type": "", "size": "", "acres": None, "lots": None, "approach": "", "section": "new",
         "acquired": None, "purchase_price": None, "acquisition_debt": None, "contributions": None,
         "distributions": None, "lost": False, "project_irr": None, "last_sale": None,
         "sold_in_parts": False, "excluded": "", "sales": [], "stage": "", "smartsheet_sold": False,
         "notes": [], "sources": {"added": source}}
    d.update({k: v for k, v in fields.items() if v not in (None, "")})
    rec["deals"].append(d)
    return d


# ─── Filling from Smartsheet and the summary cards ────────────────────────────

_STATE_NAMES = {"arizona": "AZ", "california": "CA", "texas": "TX", "colorado": "CO", "new mexico": "NM",
                "nevada": "NV", "utah": "UT", "georgia": "GA", "florida": "FL", "idaho": "ID"}


def _state_code(state: str) -> str:
    s = (state or "").strip()
    return _STATE_NAMES.get(s.lower(), s)


def sync_smartsheet(rec: dict) -> int:
    """Stage and sold flag from the Project Master; a new name becomes a new deal. Returns changes."""
    try:
        import portfolio
        active, sold = portfolio.load_all_properties()
    except Exception as e:  # no export on this machine: nothing to add, never a failure
        log.info("Deal tracker: no Project Master to read (%s)", e)
        return 0
    known = lambda v: "" if (v or "").lower() == "unknown" else (v or "")
    rows = [(p["name"], known(p.get("category")), known(p.get("state")), False) for p in active]
    rows += [(p["name"], "Sold", known(p.get("state")), True) for p in sold]
    changes = 0
    first_read = not any(d.get("stage") for d in rec["deals"])   # the first read is the starting point, not news
    for name, category, state, struck in rows:
        # A project Smartsheet lists as live is never one of the old sold deals,
        # however alike the names; only a struck-through one can be.
        current = [d for d in rec["deals"] if d["section"] != "sold"]
        hits = matches(rec, name, current) or (matches(rec, name) if struck else [])
        if len(hits) > 1:
            _note(rec, f'Smartsheet has a project called "{name}", and more than one deal in the tracker could be it: '
                       + "; ".join(h["name"] for h in hits[:5]) + ". Which one is it?")
            continue
        if not hits:
            guess = likely_same(rec, name)
            d = new_deal(rec, name, "Smartsheet Project Master", state=_state_code(state))
            _note(rec, f'Smartsheet has a project called "{name}" that the old spreadsheet does not list '
                       f'under that name, so it was added as a new deal. '
                       + (f'Is it the same deal as "{guess["name"]}"?' if guess else
                          'If the old spreadsheet has it under another name, which one?'))
            hits = [d]
            changes += 1
            if not first_read:
                log_update(rec, name, "New project in Smartsheet", source="Smartsheet")
        d = hits[0]
        if d.get("stage") != category or d.get("smartsheet_sold") != struck:
            if d.get("stage") and not first_read:
                log_update(rec, d["name"], "Marked sold in Smartsheet" if struck and not d.get("smartsheet_sold")
                           else f"Stage {d['stage']} → {category}", "Changed in Smartsheet", "Smartsheet")
            d["stage"], d["smartsheet_sold"] = category, struck
            d["sources"]["stage"] = "Smartsheet Project Master"
            changes += 1
    return changes


def sync_cards(rec: dict) -> int:
    """Purchase price and year, acreage and a recorded sale from the summaries' cards; empty fields only."""
    try:
        import summaries
        cards = summaries.load_cards(config.PROPERTY_SUMMARIES_DIR)
    except Exception as e:
        log.info("Deal tracker: summary cards unreadable (%s)", e)
        return 0
    changes = 0
    for card in cards:
        names = [card.get("property") or "", *(card.get("aliases") or [])]
        sold = card.get("outcome_status") == "sold"
        hits = []
        for n in names:
            hits = matches(rec, n, [d for d in rec["deals"] if d["section"] != "sold"]) \
                or (matches(rec, n) if sold else [])
            if hits:
                break
        if len(hits) != 1:
            continue
        d, src = hits[0], f"Property summary ({card.get('filename', '')})"
        price = _num(card.get("entry_price_usd"))
        if price and d.get("purchase_price") is None:
            d["purchase_price"] = price
            d["sources"]["purchase_price"] = src
            changes += 1
        year = card.get("entry_year")
        if isinstance(year, int) and not d.get("acquired"):
            d["acquired"] = str(year)
            d["sources"]["acquired"] = src + ", year only"
            changes += 1
        acres = _num(card.get("acres"))
        if acres and d.get("acres") is None:
            d["acres"] = acres
            d["sources"]["acres"] = src
            changes += 1
        if sold and _num(card.get("exit_price_usd")) and not d["sales"] and not d.get("last_sale"):
            # Final only if Smartsheet does not still list it as live: a summary's
            # "sold" can be one parcel of a project that carries on.
            live = bool(d.get("stage")) and not d.get("smartsheet_sold")
            d["sales"].append({"date": str(card.get("exit_year") or ""), "price": _num(card.get("exit_price_usd")),
                               "acres": None, "other_party": "", "final": not live, "source": src})
            changes += 1
    return changes


def refresh(rec: dict) -> dict:
    """The cheap sources (no document is opened). Returns counts."""
    return {"smartsheet": sync_smartsheet(rec), "cards": sync_cards(rec)}


# ─── Status, worked out from what is recorded ─────────────────────────────────

def status(d: dict) -> str:
    sold_any = bool(d["sales"]) or bool(d.get("last_sale")) or d.get("sold_in_parts")
    if d.get("lost") and not d["sales"]:
        return "Lost"
    if d["section"] == "sold" or d.get("smartsheet_sold") or any(s.get("final") for s in d["sales"]):
        return "Fully exited"
    if sold_any:
        return "Partially sold"
    if d.get("stage") == "Acquisition":
        return "Being acquired"
    return "Owned"


# ─── New documents to read ────────────────────────────────────────────────────

def do_not_open_words() -> list[str]:
    """The list beside the record, seeded with the defaults the first time."""
    try:
        if not DO_NOT_OPEN_FILE.is_file():
            DO_NOT_OPEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            DO_NOT_OPEN_FILE.write_text(
                "# Any saved file whose NAME contains one of these words is never opened by the deal\n"
                "# tracker, and never listed. One word or phrase per line. Lines starting with # are notes.\n"
                + "\n".join(DEFAULT_DO_NOT_OPEN) + "\n", encoding="utf-8")
        words = [w.strip().lower() for w in DO_NOT_OPEN_FILE.read_text(encoding="utf-8").splitlines()]
        words = [w for w in words if w and not w.startswith("#")]
        return words or list(DEFAULT_DO_NOT_OPEN)
    except OSError:
        return list(DEFAULT_DO_NOT_OPEN)


def blocked(name: str, words: list[str]) -> bool:
    # The word goes through the same cleaning as the name: "k-1" once never matched "K-1 2025.pdf".
    clean = lambda s: " ".join(re.sub(r"[_\-.]+", " ", s.lower()).split())
    low = " " + clean(name) + " "
    return any(re.search(r"(?<![a-z0-9])" + re.escape(clean(w)) + r"(?![a-z0-9])", low) for w in words if clean(w))


def _closing_folder(path: str) -> str:
    parts = path.split("/")[:-1]
    while parts and parts[-1].strip().lower() in _CLOSING_SUBFOLDERS:
        parts.pop()
    return "/".join(parts)


def _property_folder(path: str) -> str:
    """The property's folder, looking past a grouping folder such as '! SOLD'."""
    parts = path.split("/")
    if len(parts) >= 5 and parts[0].lower() == "!properties":
        return parts[3] if parts[2].startswith("!") else parts[2]
    return ""


def _best(files: list[str], side: str) -> str:
    """The statement most likely to be the final one in a closing's folder."""
    def score(p):
        n = p.rsplit("/", 1)[-1].lower()
        s = 0
        s += 6 if re.search(r"\bfinal\b|\bexecuted\b|\bfe\b|\bex\b|\bsig copy\b", n) else 0
        s -= 6 if re.search(r"\best(imated)?\b", n) else 0
        s += 2 if (side == "sale" and "seller" in n) or (side == "purchase" and "buyer" in n) else 0
        v = re.findall(r"\bv(\d+(?:\.\d+)?)\b", n)
        s += float(v[-1]) / 10 if v else 0
        return (s, n)
    return max(files, key=score)


def drive_closings(rec: dict, words: list[str]) -> list[dict]:
    """Closings filed on the drive since the record's start date that have not been read."""
    from corpus import index
    from corpus import describe as D
    con = index._connect()
    if con is None:
        return []
    try:
        rows = con.execute("SELECT path, name FROM files WHERE path LIKE '!PROPERTIES/%'").fetchall()
    finally:
        con.close()
    groups: dict[str, list[str]] = {}
    for path, name in rows:
        low = name.lower()
        if low.endswith((".msg", ".eml")) or not (_SS_NAME.search(low.replace("_", " ")) or _SS_FOLDER.search(path)):
            continue
        if _NOT_A_CLOSING.search(path.replace("_", " ")) or blocked(name, words):
            continue
        filed = D.name_date(name)
        if not filed or filed < rec["watch_from"]:
            continue
        groups.setdefault(_closing_folder(path), []).append(path)
    # The same statement is often filed in two folders; once read under one, it is read.
    read_names = {k.rsplit("/", 1)[-1] for k in rec["processed"] if k.startswith("drive:")}
    side_of = lambda folder: ("sale" if _SALE_PATH.search(folder + "/")
                              else "purchase" if _BUY_PATH.search(folder + "/") else "unclear")
    out, offered = [], set()
    # A folder that says sale or purchase first, so a copy filed in both is offered from that one.
    for folder, files in sorted(groups.items(), key=lambda g: (side_of(g[0]) == "unclear", g[0])):
        if all(("drive:" + f) in rec["processed"] for f in files):
            continue
        side = side_of(folder)
        best = _best(files, side)
        name = best.rsplit("/", 1)[-1]
        if name in read_names or name in offered:
            continue
        offered.add(name)
        out.append({"key": "drive:" + folder, "source": "drive", "side": side,
                    "property_folder": _property_folder(best), "document": best,
                    "filed": max(D.name_date(f.rsplit("/", 1)[-1]) for f in files),
                    "others": sorted(f.rsplit("/", 1)[-1] for f in files if f != best), "files": files})
    return out


# Ava's flow saves EVERY attachment (Microsoft's own template, so she sets no filters;
# owner, 2026-11-05: the setup must be easy for a non-technical person). So the
# tracker picks: a deal paper by its name, or a calendar note ("Calendar - <title>.txt",
# saved only for one colleague's meetings) whose title names a date. Anything else is never opened.
PAPER_WORDS = ["loi", "letter of intent", "psa", "purchase agreement", "purchase and sale", "counter",
               "amendment", "settlement statement", "closing statement"]
CALENDAR_PREFIX = "calendar - "
CALENDAR_WORDS = ["deadline", "expiration", "expires", "expire", "due", "confirm", "confirmed", "closing", "close",
                  "dd", "feasibility"]


def _wanted(name: str) -> bool:
    if name.lower().startswith(CALENDAR_PREFIX):
        return blocked(name[len(CALENDAR_PREFIX):], CALENDAR_WORDS)
    return blocked(name, PAPER_WORDS)


# A handed-in file is SCREENED on this machine before Claude ever sees it (owner,
# 2026-11-05: read files without the keywords too, but stop the moment one holds
# confidential information and leave it out). The code reads the opening text; a
# confidential phrase ends it there. Phrases are ones a deal paper does not carry:
# a PSA mentions a 1099-S or a W-9 as paperwork, so those are deliberately absent.
CONFIDENTIAL_TEXT = ["schedule k-1", "partner's share of income", "form w-2", "wage and tax statement",
                     "social security number", "date of birth", "bank statement", "beginning balance",
                     "ending balance", "general ledger", "trial balance", "balance sheet", "profit and loss",
                     "income statement", "payroll", "pay stub", "capital account statement",
                     "investor statement", "attorney-client privileged", "privileged and confidential"]
# A file with no deal word in its name is offered only if its text reads like a deal
# paper: at least two of these.
DEAL_SIGNALS = ["letter of intent", "purchase and sale", "purchase agreement", "purchase contract",
                "settlement statement", "closing statement", "earnest money", "due diligence",
                "feasibility period", "counter offer", "counteroffer", "amendment to", "close of escrow",
                "closing date", "purchase price", "purchaser", "assignment of contract"]
SCREEN_CHARS = 12_000          # the opening pages; enough to tell, never the whole file
SCREEN_BUDGET_SECONDS = 20     # per look at the folders; what is left is screened next time


def _screen(p: Path, prefix: str) -> str:
    """'deal', 'other' or 'confidential'. Only 'deal' is ever offered to Claude."""
    from corpus import extract
    named = prefix == "drop" or _wanted(p.name)
    try:
        text = extract.read_path(p, p.name, max_chars=SCREEN_CHARS)[0].lower()
    except Exception:
        return "deal" if named else "other"   # unreadable: a named paper is still worth Claude's try
    text = " ".join(text.replace("’", "'").split())
    if any(w in text for w in CONFIDENTIAL_TEXT):
        return "confidential"
    if named:
        return "deal"
    return "deal" if sum(w in text for w in DEAL_SIGNALS) >= 2 else "other"


def _screen_id(key: str) -> str:
    # The record sits in the TEAM folder, so a screened-out file is kept by a fingerprint,
    # never its name: the name of somebody's personal attachment is not the team's business.
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


INBOX_QUIET_DAYS = 30


def note_inbox(rec: dict) -> None:
    """
    On the machine with the email folder only: when anything last arrived in it.
    A flow that stopped (a changed password, say) just goes quiet; the page warns.
    """
    inbox = config.DEAL_INBOX_DIR
    if not inbox:
        return
    newest = None
    if inbox.is_dir():
        times = [p.stat().st_mtime for p in inbox.rglob("*") if p.is_file()]
        newest = _dt.date.fromtimestamp(max(times)).isoformat() if times else None
    rec["inbox_check"] = {"checked": _dt.date.today().isoformat(), "found": inbox.is_dir(), "newest": newest}


def inbox_warning(rec: dict, today: _dt.date) -> str:
    c = rec.get("inbox_check")
    if not c:
        return ""
    if not c.get("found"):
        return "The email folder on the daily-run computer is missing, so no email papers are coming in."
    if not c.get("newest") or (today - _dt.date.fromisoformat(c["newest"])).days > INBOX_QUIET_DAYS:
        since = f" since {c['newest']}" if c.get("newest") else ""
        return (f"Nothing has arrived from email{since}. The email flow may have stopped: "
                "check it is On in Power Automate.")
    return ""


# The two folders a file is handed in through: (key prefix, folder, what to call it).
def _hand_in_folders() -> list[tuple[str, Path | None, str]]:
    return [("inbox", config.DEAL_INBOX_DIR, "email attachment"), ("drop", config.DEAL_DROP_DIR, "drop folder")]


def inbox_files(rec: dict, words: list[str]) -> list[dict]:
    """Saved email attachments (one machine only) and papers dropped in by hand, not yet read."""
    before = len(rec.get("screened", {}))
    deadline = time.monotonic() + SCREEN_BUDGET_SECONDS
    out = [x for prefix, root, source in _hand_in_folders()
           for x in _folder_files(rec, words, prefix, root, source, deadline)]
    if len(rec.get("screened", {})) != before:
        save(rec)   # a screen is a few seconds of reading; never do one twice
    return out


def _folder_files(rec: dict, words: list[str], prefix: str, inbox: Path | None, source: str,
                  deadline: float) -> list[dict]:
    from corpus.extract import SUPPORTED_EXTENSIONS
    if not inbox or not inbox.is_dir():
        return []
    out = []
    screened = rec.setdefault("screened", {})
    for p in sorted(inbox.rglob("*")):
        if not p.is_file() or p.name.startswith(("~", ".")) or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        if prefix == "drop" and config.DEAL_DROP_READ_NAME in p.relative_to(inbox).parts[:-1]:
            continue
        if blocked(p.name, words):
            continue
        st = p.stat()
        key = f"{prefix}:{p.relative_to(inbox).as_posix()}|{st.st_size}"
        if key in rec["processed"]:
            continue
        sid = _screen_id(key)
        if sid not in screened:
            if time.monotonic() > deadline:
                continue
            screened[sid] = _screen(p, prefix)
            if screened[sid] == "confidential" and (prefix == "drop" or _wanted(p.name)):
                # Named like a deal paper, so somebody may be waiting on it: say so, by its name only.
                rec.setdefault("attention", []).append(
                    f'"{p.name}" looks like a deal paper but holds confidential information, so it was not read. '
                    "Record what it says by hand if it matters.")
        if screened[sid] != "deal":
            continue
        out.append({"key": key, "source": source, "side": "unclear", "property_folder": "",
                    "document": p.relative_to(inbox).as_posix(), "path": str(p),
                    "filed": _dt.date.fromtimestamp(st.st_mtime).isoformat(), "others": [], "files": [key]})
    return out


def pending(rec: dict) -> list[dict]:
    from deal_tracker import pipeline
    words = do_not_open_words()
    papers = pipeline.drive_papers(rec, words) if rec.get("pipeline") is not None else []
    return inbox_files(rec, words) + drive_closings(rec, words) + papers


def read_text(item: dict, max_chars: int = 9000) -> str:
    from corpus import extract
    if item["source"] == "drive":
        text, _ = extract.read_document(item["document"], max_chars=max_chars)
    else:
        roots = [r.resolve() for _, r, s in _hand_in_folders() if r and s == item["source"]]
        p = Path(item["path"]).resolve()
        if not any(r in p.parents for r in roots):
            raise ValueError("That file is not in a folder the tracker reads.")
        text, _ = extract.read_path(p, p.name, max_chars=max_chars)
    return text


def mark_read(rec: dict, item_key: str, result: str, deal_id: str = "") -> bool:
    """Mark every file of a closing (or the one attachment) as read. False if the key is unknown."""
    stamp = {"at": _dt.datetime.now().isoformat(timespec="seconds"), "result": result, "deal": deal_id}
    if item_key.startswith("inbox:"):
        rec["processed"][item_key] = stamp
        return True
    if item_key.startswith("drop:"):
        rec["processed"][item_key] = stamp
        _file_away(item_key[len("drop:"):].rsplit("|", 1)[0])
        return True
    if item_key.startswith("paper:"):
        from deal_tracker import pipeline
        found = [i for i in pipeline.drive_papers(rec, do_not_open_words()) if i["key"] == item_key]
        if found:
            pipeline.mark_read(rec, found[0], result, deal_id)
        return bool(found)
    found = [i for i in drive_closings(rec, do_not_open_words()) if i["key"] == item_key]
    if not found:
        return False
    for f in found[0]["files"]:
        rec["processed"]["drive:" + f] = stamp
    return True


def _file_away(rel: str) -> None:
    """Move a dropped paper into Read/ once recorded. A file open elsewhere stays put (it is marked read)."""
    src = config.DEAL_DROP_DIR / rel
    dest_dir = config.DEAL_DROP_DIR / config.DEAL_DROP_READ_NAME
    dest = dest_dir / src.name
    n = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
        n += 1
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        src.replace(dest)
    except OSError:
        pass


# ─── Recording what a document says ───────────────────────────────────────────

def record_event(rec: dict, d: dict, kind: str, date: str, price, acres, other_party: str,
                 final: bool, contributions, distributions, source: str) -> list[str]:
    """Apply one purchase or sale to a deal. Returns what changed, and any disagreement."""
    said = []
    date = (date or "").strip()
    if kind == "purchase":
        for field, val in (("acquired", date or None), ("purchase_price", _num(price)), ("acres", _num(acres))):
            if val in (None, ""):
                continue
            old = d.get(field)
            # A signed closing statement outranks a figure taken from a summary's card,
            # and a difference under a dollar is rounding, not a disagreement.
            weaker = str(d["sources"].get(field, "")).startswith("Property summary")
            rounding = isinstance(old, float) and isinstance(val, float) and abs(old - val) < 1
            if old in (None, "") or weaker or rounding or \
                    (field == "acquired" and len(str(old)) == 4 and str(val).startswith(str(old))):
                d[field] = val
                d["sources"][field] = source
                said.append(f"{field.replace('_', ' ')} set")
            elif str(old) != str(val):
                _note(rec, f'{d["name"]}: {source} gives {field.replace("_", " ")} {val}, the record has {old}. '
                           f"The record was kept; correct it if the document is right.")
                said.append(f"{field.replace('_', ' ')} disagrees (kept the old one)")
        if other_party:
            d["sources"]["seller"] = other_party
    elif kind == "sale":
        ev = {"date": date, "price": _num(price), "acres": _num(acres), "other_party": other_party.strip(),
              "final": bool(final), "source": source}
        # The buyer is part of "the same sale": one property can close twice on one day at one
        # price to two builders (seen on a real two-closing sale, 2026-10-29).
        if any(s["date"] == ev["date"] and s["price"] == ev["price"]
               and _tokens(s.get("other_party", "")) == _tokens(ev["other_party"]) for s in d["sales"]):
            said.append("sale already recorded")
        else:
            d["sales"].append(ev)
            d["sales"].sort(key=lambda s: s["date"])
            if date and (not d.get("last_sale") or date > d["last_sale"]):
                d["last_sale"] = date
            said.append("sale added" + (", marked as the final sale" if final else ""))
            log_update(rec, d["name"], " ".join(x for x in (
                "Sold" + (" the last of it" if final else " part"),
                f"{ev['acres']:g} acres" if ev["acres"] else "", f"for {plain_money(ev['price'])}" if ev["price"] else "",
                f"to {ev['other_party']}" if ev["other_party"] else "", f"on {plain_date(date)}" if date else "") if x),
                "Closing statement filed", source)
    if kind == "purchase" and any(s.endswith(" set") for s in said):
        log_update(rec, d["name"], " ".join(x for x in (
            "Bought", f"for {plain_money(d['purchase_price'])}" if d.get("purchase_price") else "",
            f"on {plain_date(d['acquired'])}" if d.get("acquired") and len(str(d["acquired"])) == 10 else "") if x),
            "Closing statement filed", source)
    for field, val in (("contributions", contributions), ("distributions", distributions)):
        val = _num(val)
        if val is None:
            continue
        d[field] = val
        d["sources"][field] = source
        said.append(f"{field} updated")
    return said
