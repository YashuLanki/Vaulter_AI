"""
analysis/screening/report.py
----------------------------
Turn a screening result into a single self-contained HTML report.

Replaces the old `dashboard_server.py`, which is retired. That served the
workbook over a local HTTP server on a background daemon thread, and had two
problems beyond needing a thread the rebuild had otherwise eliminated: it read
sheet names (`Phase1_Screening`, `Phase2_Ranked`, …) the current screener no
longer writes, so it displayed nothing at all; and a colleague could not open
the result without running the server themselves.

A single file with its data inlined has neither problem. It lands next to the
workbook in the shared folder, so anyone on the team can open it straight from
OneDrive, and it keeps working when the screener's internals change again.

Layered for three readers, top to bottom:
  * the decision — three candidates, the money, the concentration;
  * the map and shortlist — where they are, and how each one scored;
  * the model — every listing, and every assumption behind the ranking.

Clicking anything opens the same detail view, so there is one place to learn
what a property is rather than four partial ones.

Two things travel with every report and are not optional:

  * the ARITHMETIC behind each pricing score — ask, entitlement, carry, the exit
    it therefore has to fetch, and what the market pays for that product. A
    score with its working hidden cannot be argued with, and non-residential
    rows carry no entitlement figure at all, which only `Cost_Basis` says.
  * how complete the EXPORT was. `column_sources` records what was found under
    another name, derived, or absent; `evidence_coverage` records what the
    portfolio can say about these markets. Without them a 24-column file with
    no coordinates and acreage on 5 of 50 rows produced a page that looked
    exactly as confident as a 216-row one.
"""

import json
import logging
import math
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("vaulter.report")

TEMPLATE = Path(__file__).with_name("report_template.html")

# The basemap is fetched once per geography and cached: county, city and road
# vectors do not change between screening runs, and re-fetching them would add
# ten seconds to every report for no benefit.
_BASEMAP_CACHE_VERSION = 1


def _round_bbox(bbox, step=0.5):
    """Snap a bounding box outward to a grid so nearby exports share a cache
    entry instead of each fetching their own near-identical basemap."""
    x0, y0, x1, y1 = bbox
    return (math.floor(x0 / step) * step, math.floor(y0 / step) * step,
            math.ceil(x1 / step) * step, math.ceil(y1 / step) * step)


def _basemap_cache_path(bbox) -> Path:
    from config import SHARED_DIR, GEO_CACHE_DIR
    d = GEO_CACHE_DIR
    d.mkdir(parents=True, exist_ok=True)
    tag = "_".join(f"{v:+.1f}" for v in bbox)
    return d / f"basemap_v{_BASEMAP_CACHE_VERSION}_{tag}.json"


def _simplify(points, tol):
    """Ramer-Douglas-Peucker. Boundary detail far below a screen pixel is
    payload weight and nothing else."""
    if len(points) < 3:
        return points

    def dist(p, a, b):
        if a == b:
            return math.dist(p, a)
        t = max(0, min(1, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) /
                       ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2)))
        return math.dist(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))

    worst, idx = 0, 0
    for i in range(1, len(points) - 1):
        d = dist(points[i], points[0], points[-1])
        if d > worst:
            worst, idx = d, i
    if worst > tol:
        return _simplify(points[:idx + 1], tol)[:-1] + _simplify(points[idx:], tol)
    return [points[0], points[-1]]


def _shapes(features, tol, key, kind):
    out = []
    for f in features or []:
        name = (f.get("attributes") or {}).get(key) or ""
        for part in (f.get("geometry") or {}).get(kind, []):
            s = _simplify([(round(p[0], 4), round(p[1], 4)) for p in part], tol)
            if len(s) > (3 if kind == "rings" else 1):
                out.append({"n": name, "r": s})
    return out


def build_basemap(bbox, use_cache: bool = True) -> dict:
    """
    County outlines, incorporated places and the road network for a bounding
    box, from Census TIGERweb.

    Vectors rather than map tiles. A tiled basemap would look more familiar,
    but it needs a live connection every time the file is opened and cannot be
    shared as one self-contained document; embedded vectors travel with it.
    """
    from analysis.screening.geo_federal import _arcgis

    bbox = _round_bbox(bbox)
    cache = _basemap_cache_path(bbox)
    if use_cache and cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    T = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
    geom = {"xmin": bbox[0], "ymin": bbox[1], "xmax": bbox[2], "ymax": bbox[3],
            "spatialReference": {"wkid": 4326}}
    common = {"geometry": json.dumps(geom), "geometryType": "esriGeometryEnvelope",
              "inSR": "4326", "outSR": "4326",
              "spatialRel": "esriSpatialRelIntersects", "returnGeometry": "true"}

    layers = [
        ("counties", f"{T}/State_County/MapServer/13/query", "BASENAME", 0.010, "rings"),
        ("places",   f"{T}/Places_CouSub_ConCity_SubMCD/MapServer/4/query", "BASENAME", 0.004, "rings"),
        ("roads",    f"{T}/Transportation/MapServer/2/query", "NAME", 0.006, "paths"),
        ("roads2",   f"{T}/Transportation/MapServer/4/query", "NAME", 0.008, "paths"),
    ]
    out = {}
    for name, url, field, tol, kind in layers:
        data = _arcgis(url, {**common, "outFields": field})
        out[name] = _shapes((data or {}).get("features"), tol, field, kind)
        if data is None:
            log.warning(f"Basemap layer '{name}' unavailable — the map will omit it.")
    try:
        cache.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    except OSError as e:
        log.warning(f"Could not cache basemap: {e}")
    return out


def fetch_imagery(listings, limit: int = 12) -> dict:
    """
    Aerial photography for the top listings, inlined as data URIs.

    Off by default in build_report: each site costs a STAC search plus an image
    fetch, so a dozen adds a couple of minutes to a run that is otherwise
    instant. Worth it for a report someone will actually present.
    """
    import base64
    import io

    from analysis.screening import geo_providers as gp

    out = {}
    for x in listings[:limit]:
        if not x.get("lat"):
            continue
        try:
            r = gp.satellite_image(x["lat"], x["lng"])
            if r.get("status") != "OK" or not r.get("image_bytes"):
                continue
            from PIL import Image
            im = Image.open(io.BytesIO(r["image_bytes"])).convert("RGB")
            im.thumbnail((420, 420), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=74, optimize=True)
            out[str(x["rank"])] = {
                "src": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode(),
                "captured": (r.get("captured") or "")[:10],
            }
        except Exception as e:
            log.warning(f"No imagery for rank {x.get('rank')}: {e}")
    return out


# ─── Shaping the screener's frame for the page ────────────────────────────────

def _num(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _str(v) -> str:
    """Text for the page. A missing text column arrives as float NaN, and NaN is
    truthy, so `str(v or "")` yields the literal string 'nan' on the page."""
    if v is None or v != v:
        return ""
    return str(v)


def _listing(row) -> dict:
    g = row.get
    return {
        # _str, not `str(v or "")`, on every text field: a blank cell arrives as
        # float NaN, NaN is truthy, so `or` never fires and the page printed the
        # literal string "nan". The 216-row Phoenix export has one such row, and
        # it rendered as a listing addressed "nan".
        "rank": int(g("Rank")), "tier": _str(g("Fit_Tier")),
        "score": _num(g("Fit_Score")),
        "addr": _str(g("Property Address")) or "(no address)",
        "city": _str(g("City")), "county": _str(g("County Name")),
        "state": _str(g("State")),
        "acres": _num(g("Land Area (AC)")), "price": _num(g("For Sale Price")),
        "type": _str(g("Secondary Type")),
        "lat": _num(g("Latitude")), "lng": _num(g("Longitude")),
        "dist": _num(g("Distance_Mi")),
        "nearest": _str(g("Nearest_Holding")),
        "askPerAcre": _num(g("Ask_Per_Acre")),
        "dom": _num(g("Days On Market")),
        "distress": _str(g("Distress_Signals")),
        "cautions": _str(g("Cautions")),
        "why": _str(g("Why")),

        # The 2026-07-28 cost rework, which the page did not carry. Without
        # these it showed a pricing score and hid the arithmetic behind it —
        # and, worse, hid the fact that every non-residential row is costed
        # with NO entitlement figure at all, so its required exit is understated.
        # Cost_Basis is the sentence that says so, and it has to travel with
        # the number it qualifies.
        "read": _str(g("Vaulter_Read")),
        "history": _str(g("Portfolio_Comparison")),
        "costBasis": _str(g("Cost_Basis")),
        "entPerAcre": _num(g("Entitlement_Per_Acre")),
        "carryPerAcre": _num(g("Carry_Per_Acre")),
        "reqExit": _num(g("Required_Exit_Per_Acre")),
        "exitComp": _num(g("Exit_Comp_Per_Acre")),
        "headroom": _num(g("Exit_Headroom")),
        "conf": _str(g("Pricing_Confidence")),
        "compN": _num(g("Exit_Comp_N")),
        "compBasis": _str(g("Exit_Comp_Basis")),
        "verdict": _str(g("Pricing_Verdict")),
        "perLot": _num(g("Implied_Exit_Per_Lot")),

        "comp": {"proximity": _num(g("Score_Proximity")), "pricing": _num(g("Score_Pricing")),
                 "distress": _num(g("Score_Distress")), "size": _num(g("Score_Size"))},
    }


# `IRR_at_3x_Underwritten_4yr`, `IRR_at_3x_ActualHist_14yr` — the multiple and
# the hold lengths are all assumption-driven, so the column names are read
# rather than reconstructed.
_IRR_COL = re.compile(r"^IRR_at_[\d.]+x_(.+?)_(\d+)yr$")


def _irr(df) -> list:
    """Implied IRR at each modelled horizon. Constant across rows — it depends
    only on the target multiple and the hold — so the first row is enough."""
    out = []
    if not len(df):
        return out
    for c in df.columns:
        m = _IRR_COL.match(str(c))
        if m:
            out.append({"label": m.group(1), "years": int(m.group(2)),
                        "pct": _num(df[c].iloc[0])})
    return sorted(out, key=lambda d: d["years"])


def build_report(result: dict, out_path: Path = None, include_imagery: bool = False,
                 verified: dict = None) -> Path:
    """
    Write the HTML report for a `fit_screen.screen()` result.

    Args:
        result:          what screen() returned
        out_path:        defaults to beside the workbook in the shared folder
        include_imagery: fetch aerial photography for the top listings. Slow
                         (a couple of minutes); off by default.
        verified:        optional {address: {...}} from geo_federal checks
    """
    from config import SCREENING_OUTPUT_DIR
    from portfolio import load_properties  # noqa: F401  (kept for parity of imports)
    from analysis.screening.fit_screen import load_holdings

    df = result["dataframe"]
    listings = [_listing(r) for _, r in df.iterrows()]

    holdings_df = load_holdings()
    holdings = [{"name": h.property_name, "state": h.state,
                 "lat": float(h.latitude), "lng": float(h.longitude)}
                for h in holdings_df.itertuples()]

    pts = [(x["lat"], x["lng"]) for x in listings if x["lat"] and x["lng"]]
    if pts:
        lats = [p[0] for p in pts]
        lngs = [p[1] for p in pts]
        bbox = (min(lngs) - 0.3, min(lats) - 0.3, max(lngs) + 0.3, max(lats) + 0.3)
        basemap = build_basemap(bbox)
    else:
        basemap = {"counties": [], "places": [], "roads": [], "roads2": []}

    counties = df["County Name"].value_counts().to_dict() if "County Name" in df else {}
    top10 = df.head(10)["County Name"].value_counts().to_dict() if "County Name" in df else {}
    price = df["For Sale Price"] if "For Sale Price" in df else None

    payload = {
        "source": result["source"], "screened": result["total_screened"],
        "moic": result["moic_target"], "markets": result["markets"],
        "holdingsUsed": result["holdings_used"],
        "tierCounts": {str(k): int(v) for k, v in result["tier_counts"].items()},
        "assumptions": result["assumptions"], "weights": result["weights_used"],
        "countyAll": {str(k): int(v) for k, v in counties.items()},
        "countyTop10": {str(k): int(v) for k, v in top10.items()},
        "capitalTop3": float(price.head(3).sum()) if price is not None else 0.0,
        "capitalTop3N": int(price.head(3).notna().sum()) if price is not None else 0,
        "capitalTop10": float(price.head(10).sum()) if price is not None else 0.0,
        "listings": listings, "holdings": holdings,
        "verified": verified or {},

        # How complete the export was, and what the portfolio can say about the
        # markets in it. Without these a 24-column file with no coordinates, no
        # days-on-market and acreage on 5 of 50 rows produced a page that looked
        # exactly as confident as a full one. `.get` rather than `[]`: an older
        # cached result dict must still render, minus these blocks, rather than
        # raising mid-rollout.
        "columnSources": result.get("column_sources", []),
        "evidenceCoverage": result.get("evidence_coverage", []),
        "portfolioCoverage": result.get("portfolio_coverage"),
        "exitLotComps": result.get("exit_lot_comps", []),
        "irr": _irr(df),

        "generated": datetime.now().strftime("%d %B %Y"),
    }

    imagery = fetch_imagery(listings) if include_imagery else {}

    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("/*__BASEMAP__*/", "const BASE=" + json.dumps(basemap, separators=(",", ":")) + ";")
    html = html.replace("/*__IMAGERY__*/", "const IMG=" + json.dumps(imagery, separators=(",", ":")) + ";")
    html = html.replace("/*__DATA__*/", "const DATA=" + json.dumps(payload, separators=(",", ":"), default=str) + ";")

    if out_path is None:
        # One report per source file, overwritten each run -- not timestamped.
        # Same fix as fit_screen.py's workbook and pipeline/proximity_tool.py's
        # exports, all confirmed 2026-07-29 to have accumulated unboundedly in
        # the shared OneDrive folder otherwise. The "generated" date inside the
        # report (above) still shows when THIS copy was built.
        stem = Path(result["source"]).stem
        out_path = Path(SCREENING_OUTPUT_DIR) / f"screen_{stem}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    log.info(f"Report written to {out_path}")
    return out_path
