"""
Facts from the firm's Smartsheet project dashboards (owner, 2026-11-03: "you
can grab information from the dashboard as well").

Each project has a dashboard built from one template, whose "Project Overview"
box states the company, place, cross streets, parcel numbers, zoning, density,
acreage, lot count and lot sizes. Read through the Smartsheet API, read-only,
with the key in this machine's own confidentials/.env: the daily run's machine
(owner's choice, option (a)) and the maintainer's. A machine with no key skips
this quietly; nothing else depends on it.

Two things are never read, by rule rather than by care: budget, spending and
contingency boxes (accounting, which the owner put off limits), and the
investors line (people's names, on a page the whole team sees). So the labels
read are an ALLOWLIST: a box added to the template later is left out until
someone adds it here on purpose.

It runs from the daily task, not when the tracker is opened: one request per
dashboard is about a second each, too long for a tool call.

What it fills follows the tracker's rules. The dashboard is the team's own
living record of the land, so its place, zoning and the like replace what was
there, and a real change (a rezoning) goes in "What changed". Acreage and lot
count only fill an empty field; a disagreement with a figure already recorded
is listed for a person. A dashboard matching no deal, or two, is listed too:
never guessed.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re

from deal_tracker import tracker as T

log = logging.getLogger(__name__)
API = "https://api.smartsheet.com/2.0"

LABELS = {  # dashboard label (lower case, no colon) -> what it fills
    "entity name": "entity", "city/state/county": "place", "cross-streets": "cross_streets",
    "assessor parcel no": "apn", "zoning": "zoning", "density": "density", "acreage": "acres",
    "lot count": "lots", "lot size": "lot_sizes"}
WIDGETS = {"project overview", "project name"}   # the stage comes from the Project Master
TEXT = {"cross_streets": "Cross streets", "apn": "Parcel numbers", "zoning": "Zoning", "density": "Density",
        "lot_sizes": "Lot sizes", "city": "City", "county": "County"}


def _get(path: str, token: str, **params):
    import requests
    r = requests.get(API + path, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _clean(v) -> str:
    """One line; a dashboard's line breaks (two parcel numbers, two lot sizes) become "; "."""
    lines = [" ".join(x.split()) for x in str(v or "").replace("\r", "\n").split("\n")]
    return "; ".join(x for x in lines if x)


_COMPANY = re.compile(r"\b(llc|lp|lllp|llp|inc)\b\.?", re.I)


def read_one(sight: dict) -> dict:
    """The allowed facts on one dashboard, by its widgets."""
    out = {"dashboard": sight.get("name", "")}
    for w in sight.get("widgets") or []:
        if (w.get("title") or "").strip().lower() not in WIDGETS:
            continue
        for x in (w.get("contents") or {}).get("cellData") or []:
            label = (x.get("label") or w.get("title") or "").strip().rstrip(":").lower()
            value = _clean((x.get("cell") or {}).get("displayValue"))
            if label == "project name" and value:
                out["project"] = value
            elif label in LABELS and value:
                out[LABELS[label]] = value
    out.setdefault("project", re.sub(r"^\s*\d+\.\s*|\s*dashboard\s*$", "", out["dashboard"], flags=re.I).strip())
    return out


def read_all(token: str) -> list[dict]:
    """Every dashboard this account may see. A dashboard that will not load is skipped and logged."""
    found = []
    for s in _get("/sights", token, includeAll="true").get("data", []):
        try:
            found.append(read_one(_get(f"/sights/{s['id']}", token, level=4)))
        except Exception as e:
            log.warning("Smartsheet dashboard %s could not be read: %s", s.get("name"), e)
    return found


def _number(v):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(v or ""))
    return float(m.group(0).replace(",", "")) if m else None


def apply(rec: dict, found: list[dict]) -> int:
    """Fill the matching deals. Returns how many fields changed."""
    changes = 0
    for f in found:
        name = f.get("project") or ""
        hits = T.matches(rec, name, [d for d in rec["deals"] if d["section"] != "sold"]) or T.matches(rec, name)
        if len(hits) != 1:
            T._note(rec, f'The Smartsheet dashboard "{f["dashboard"]}" '
                         + (f"could be any of: {'; '.join(h['name'] for h in hits[:5])}. Which deal is it?"
                            if hits else "matches no deal in the tracker. Which deal is it?"))
            continue
        d, src = hits[0], f'Smartsheet dashboard ({f["dashboard"]})'
        # A deal still known by its company name takes the project's name, as a Smartsheet match does.
        if _COMPANY.search(d["name"]) and not _COMPANY.search(name):
            old = d["name"]
            d["name"] = name
            T.link_name(rec, old, d)
        if f.get("entity"):
            T.link_name(rec, f["entity"], d)
        vals = {k: f[k] for k in ("cross_streets", "apn", "zoning", "density", "lot_sizes") if f.get(k)}
        parts = [p.strip() for p in str(f.get("place") or "").split("/")]
        if len(parts) == 3:
            vals.update({k: v for k, v in zip(("city", "state", "county"), parts) if v})
        news = []
        for k, v in vals.items():
            old = d.get(k)
            if old != v:
                if old and k in TEXT:
                    news.append(f"{TEXT[k]} {old} → {v}")
                d[k] = v
                d["sources"][k] = src
                changes += 1
        for k, label in (("acres", "acreage"), ("lots", "lot count")):
            v = _number(f.get(k))
            if v is None:
                continue
            old = d.get(k)
            if old is None:
                d[k] = v if k == "acres" else int(v)
                d["sources"][k] = src
                changes += 1
            elif abs(float(old) - v) > max(0.5, 0.01 * v):
                T._note(rec, f'{d["name"]}: the Smartsheet dashboard gives {label} {v:g}, the tracker has {old:g}. '
                             "The tracker's figure was kept; correct it if the dashboard is right.")
        if news:
            T.log_update(rec, d["name"], "; ".join(news), "Changed on the Smartsheet dashboard", "Smartsheet dashboard")
    rec["dashboards_read"] = _dt.date.today().isoformat()
    return changes


def refresh(rec: dict) -> int:
    """Read and apply, if this machine has the Smartsheet key; 0 and nothing done otherwise."""
    import config
    token = getattr(config, "SMARTSHEET_ACCESS_TOKEN", "")
    if not token:
        return 0
    try:
        return apply(rec, read_all(token))
    except Exception as e:  # unreachable Smartsheet: the rest of the daily run carries on
        log.warning("Smartsheet dashboards not read: %s", e)
        return 0
