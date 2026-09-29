"""
Growth signals for the screener: is this listing somewhere that is going
anywhere?

Built 2026-09-29 on the first written disagreement with a ranking this screener
ever received. A teammate reported that ranking on closeness to the firm's
existing sites "isn't necessarily something we base an acquisition off of", and
that re-weighting toward growth -- new and proposed developments, school
quality, population growth, freeway access, interchanges, airports -- gave
results "much more aligned with our acquisition strategy". Measured on her own
export, closeness had not been the heaviest factor there, it had been the ONLY
one: acreage on 5 of 88 rows, no days-on-market, no distress signal. So this is
not a refinement; on files like hers it is the only signal left.

Four things are measured. Everything else she named stays where a judgement
belongs, in the conversation and the jurisdiction dossiers.

  * Freeway access -- miles to the nearest PRIMARY road (interstates and
    other limited-access highways) in the Census TIGER road layer. Reuses the
    road outlines the HTML report already fetches and caches per half-degree
    grid cell, so this adds NO network calls of its own to a screen. Reported
    honestly as "within the map extent": a listing whose nearest freeway lies
    outside the export's own bounding box reads as "over N miles", never as a
    made-up number. Interchanges specifically are NOT measured -- TIGER does
    not carry them as points -- and the basis text says so.
  * Airports -- miles to the nearest airport with scheduled passenger
    service, from a bundled public-domain table (OurAirports, filtered to the
    505 US large and medium airports with scheduled service). Local, offline.
  * County population growth -- percent change across the Census Population
    Estimates Program's county file (a plain download; the Census API itself
    now demands a key, which this project does not hold on principle).
  * Building permits -- residential units permitted in the latest annual
    Census Building Permits Survey county file, per 1,000 residents. The
    measurable half of "new developments": what is actually being built.

Scoring is WITHIN THE FILE, like every other market-relative figure here:
each signal becomes a percentile rank among the listings in this export, and
Score_Growth is the mean of whichever signals were available. A signal that
is constant across the file (every listing in one county) ranks every row at
50 and so contributes nothing -- it abstains rather than votes. A listing with
NO available signal scores 50, the same neutral floor proximity used, and
Growth_Basis says why.

Data files are cached in the shared geo cache (every teammate benefits from one
download), refreshed after GROWTH_CACHE_DAYS, and a stale cache is used -- and
said to be stale -- when a refresh fails. No download at all and no cache means
that signal is unavailable, never zero. Nothing here can raise out of the
screen: every failure degrades to "unavailable" text in Growth_Basis.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

log = logging.getLogger("vaulter.growth")

# What the most recent add_growth() used and could not use, for the screen
# result and the tool's completeness section. A module-level value rather
# than DataFrame.attrs, because attrs do not survive the pd.concat every later
# scoring step performs (measured: it came out None).
last_status: dict = {"used": [], "unavailable": []}

GROWTH_CACHE_DAYS = 180
_TIMEOUT = 30

PEP_URL = ("https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/"
           "counties/totals/co-est2024-alldata.csv")
BPS_URL = "https://www2.census.gov/econ/bps/County/co{year}a.txt"
# In a folder called "reference", NOT "data": release.py excludes every path
# part named "data" from an update package (that is how system/data/ stays
# local), so a table under a "data" folder would silently never reach a
# teammate, and the airport signal would read "unavailable" on every machine
# but this one. Found before shipping, by asking the packager; §25 checks it.
AIRPORTS_CSV = Path(__file__).with_name("reference") / "us_scheduled_airports.csv"

# State postal code -> (FIPS, name as the Census spells it).
STATES = {
    "AL": ("01", "Alabama"), "AK": ("02", "Alaska"), "AZ": ("04", "Arizona"),
    "AR": ("05", "Arkansas"), "CA": ("06", "California"), "CO": ("08", "Colorado"),
    "CT": ("09", "Connecticut"), "DE": ("10", "Delaware"), "DC": ("11", "District of Columbia"),
    "FL": ("12", "Florida"), "GA": ("13", "Georgia"), "HI": ("15", "Hawaii"),
    "ID": ("16", "Idaho"), "IL": ("17", "Illinois"), "IN": ("18", "Indiana"),
    "IA": ("19", "Iowa"), "KS": ("20", "Kansas"), "KY": ("21", "Kentucky"),
    "LA": ("22", "Louisiana"), "ME": ("23", "Maine"), "MD": ("24", "Maryland"),
    "MA": ("25", "Massachusetts"), "MI": ("26", "Michigan"), "MN": ("27", "Minnesota"),
    "MS": ("28", "Mississippi"), "MO": ("29", "Missouri"), "MT": ("30", "Montana"),
    "NE": ("31", "Nebraska"), "NV": ("32", "Nevada"), "NH": ("33", "New Hampshire"),
    "NJ": ("34", "New Jersey"), "NM": ("35", "New Mexico"), "NY": ("36", "New York"),
    "NC": ("37", "North Carolina"), "ND": ("38", "North Dakota"), "OH": ("39", "Ohio"),
    "OK": ("40", "Oklahoma"), "OR": ("41", "Oregon"), "PA": ("42", "Pennsylvania"),
    "RI": ("44", "Rhode Island"), "SC": ("45", "South Carolina"), "SD": ("46", "South Dakota"),
    "TN": ("47", "Tennessee"), "TX": ("48", "Texas"), "UT": ("49", "Utah"),
    "VT": ("50", "Vermont"), "VA": ("51", "Virginia"), "WA": ("53", "Washington"),
    "WV": ("54", "West Virginia"), "WI": ("55", "Wisconsin"), "WY": ("56", "Wyoming"),
}
_STATE_BY_NAME = {name.lower(): code for code, (_, name) in STATES.items()}


# ─── Small helpers ────────────────────────────────────────────────────────────

def _miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    rad = math.radians
    dlat, dlon = rad(lat2 - lat1), rad(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _state_code(value) -> str:
    """'AZ', 'az', 'Arizona' -> 'AZ'; anything else -> ''."""
    s = str(value or "").strip()
    if len(s) == 2 and s.upper() in STATES:
        return s.upper()
    return _STATE_BY_NAME.get(s.lower(), "")


def _county_key(name) -> str:
    """'Pinal County' / 'Pinal' / 'PINAL CO.' -> 'pinal'."""
    s = str(name or "").lower().strip()
    for suffix in (" county", " parish", " borough", " census area", " municipality", " co.", " co"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip()


def _cache_dir() -> Path | None:
    try:
        from config import GEO_CACHE_DIR
        Path(GEO_CACHE_DIR).mkdir(parents=True, exist_ok=True)
        return Path(GEO_CACHE_DIR)
    except Exception:
        return None


def _download_cached(name: str, url: str) -> tuple[str | None, str]:
    """
    The file's text, from the shared cache when fresh, else downloaded and
    cached. Returns (text, note) where note is "" when fresh, a plain-English
    caveat when a stale cache had to serve, and text is None when nothing
    could be had at all.
    """
    cache = _cache_dir()
    path = cache / name if cache else None
    stale_text = None
    if path is not None and path.exists():
        try:
            age_days = (time.time() - path.stat().st_mtime) / 86400
            text = path.read_text(encoding="utf-8")
            if age_days <= GROWTH_CACHE_DAYS:
                return text, ""
            stale_text = text
        except OSError:
            pass
    try:
        import requests
        r = requests.get(url, timeout=_TIMEOUT)
        if r.status_code == 200 and r.content:
            text = r.content.decode("latin-1")
            if path is not None:
                try:
                    path.write_text(text, encoding="utf-8")
                except OSError as e:
                    log.warning(f"Could not cache {name}: {e}")
            return text, ""
        log.warning(f"{url} -> HTTP {r.status_code}")
    except Exception as e:
        log.warning(f"Could not download {url}: {type(e).__name__}: {e}")
    if stale_text is not None:
        return stale_text, f"from a cached copy more than {GROWTH_CACHE_DAYS} days old"
    return None, "unreachable and not cached"


# ─── The four datasets ────────────────────────────────────────────────────────

def county_population() -> tuple[dict, str, str]:
    """
    {(state_fips, county_fips): (name, pop_first, pop_last)}, the span as
    'YYYY-YYYY', and a caveat ('' when clean).
    """
    text, note = _download_cached("growth_county_population.csv", PEP_URL)
    if text is None:
        return {}, "", note
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return {}, "", "population file was empty"
    years = sorted(int(c[len("POPESTIMATE"):]) for c in rows[0] if c.startswith("POPESTIMATE") and c[len("POPESTIMATE"):].isdigit())
    if len(years) < 2:
        return {}, "", "population file carries fewer than two years"
    first, last = f"POPESTIMATE{years[0]}", f"POPESTIMATE{years[-1]}"
    out = {}
    for r in rows:
        if r.get("COUNTY") == "000":
            continue  # the state total
        try:
            out[(r["STATE"], r["COUNTY"])] = (r["CTYNAME"], float(r[first]), float(r[last]))
        except (KeyError, ValueError):
            continue
    return out, f"{years[0]}-{years[-1]}", note


def county_permits() -> tuple[dict, int, str]:
    """
    {(state_fips, county_fips): residential units permitted}, the survey year,
    and a caveat. Tries the latest annual file first and walks back three years,
    because the Census publishes the annual file some months into the next year.
    """
    this_year = datetime.now().year
    for year in range(this_year - 1, this_year - 5, -1):
        text, note = _download_cached(f"growth_permits_{year}.csv", BPS_URL.format(year=year))
        if text is None:
            continue
        out = {}
        for line in text.splitlines()[2:]:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 18 or not parts[1].isdigit():
                continue
            try:
                # columns 7,10,13,16 are units for 1-unit, 2-unit, 3-4 unit, 5+ unit buildings
                units = sum(float(parts[i]) for i in (7, 10, 13, 16))
            except ValueError:
                continue
            out[(parts[1].zfill(2), parts[2].zfill(3))] = units
        if out:
            return out, year, note
    return {}, 0, "no annual permits file could be read for the last four years"


def airports() -> list[dict]:
    """The bundled table: [{name, ident, lat, lng}]. Empty if the file is missing."""
    try:
        with open(AIRPORTS_CSV, encoding="utf-8", newline="") as f:
            return [{"name": r["name"], "ident": r["ident"],
                     "lat": float(r["latitude"]), "lng": float(r["longitude"])}
                    for r in csv.DictReader(f)]
    except (OSError, ValueError, KeyError) as e:
        log.warning(f"Airport table unreadable: {e}")
        return []


def _points_in(shape) -> list:
    """Every [lng, lat] pair anywhere inside a nested list structure."""
    pts = []
    stack = [shape]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            if (len(node) == 2 and all(isinstance(v, (int, float)) for v in node)):
                pts.append((float(node[0]), float(node[1])))
            else:
                stack.extend(node)
    return pts


def primary_road_points(bbox) -> tuple[list, str]:
    """
    Vertices of the primary-road outlines the report already caches for this
    map extent, as (lng, lat). Empty with a caveat if the layer is unavailable.
    """
    try:
        from analysis.screening.report import build_basemap
        base = build_basemap(bbox)
    except Exception as e:
        return [], f"road layer unavailable ({type(e).__name__})"
    roads = base.get("roads") if isinstance(base, dict) else None
    if not roads:
        return [], "road layer unavailable for this map extent"
    return _points_in(roads), ""


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _pct(series: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Percentile rank 0-100 within the file; NaN stays NaN; a constant is 50."""
    s = pd.to_numeric(series, errors="coerce")
    if s.notna().sum() == 0:
        return s
    ranked = s.rank(pct=True, method="average", ascending=higher_is_better)
    # rank(pct=True) with method="average" puts a constant series at 1.0; a
    # constant must sit at the neutral middle, so it neither lifts nor drags.
    if s.dropna().nunique() == 1:
        ranked = ranked.where(s.isna(), 0.5)
    return (ranked * 100).round(1)


def add_growth(df: pd.DataFrame) -> pd.DataFrame:
    """
    Attach the four measured signals, Score_Growth and Growth_Basis. Never
    raises: every failure becomes text in Growth_Basis and a neutral score.
    Also records what was used, and what was not, in df.attrs["growth_status"].
    """
    n = len(df)
    empty = pd.Series([float("nan")] * n, index=df.index, dtype=float)
    status = {"used": [], "unavailable": []}
    global last_status
    last_status = status
    try:
        return _add_growth(df, status)
    except Exception as e:  # the screen must never die on a growth lookup
        log.error(f"Growth signals failed: {type(e).__name__}: {e}", exc_info=True)
        status["used"], status["unavailable"] = [], [f"all growth signals ({type(e).__name__})"]
        out = pd.concat([df, pd.DataFrame({
            "Freeway_Mi": empty, "Airport_Mi": empty, "Nearest_Airport": [""] * n,
            "County_Pop_Growth_Pct": empty, "County_Permits_Per_1k": empty,
            "Growth_Basis": [f"growth signals unavailable ({type(e).__name__})"] * n,
            "_growth_score": [50.0] * n}, index=df.index)], axis=1)
        out.attrs["growth_status"] = {"used": [], "unavailable": [f"all ({type(e).__name__})"]}
        return out


def _add_growth(df: pd.DataFrame, status: dict) -> pd.DataFrame:
    n = len(df)
    idx = df.index
    lat = pd.to_numeric(df.get("Latitude", pd.Series([float("nan")] * n, index=idx)), errors="coerce")
    lng = pd.to_numeric(df.get("Longitude", pd.Series([float("nan")] * n, index=idx)), errors="coerce")
    state = df.get("State", pd.Series([""] * n, index=idx)).map(_state_code)
    county = df.get("County Name", pd.Series([""] * n, index=idx)).map(_county_key)
    have_xy = lat.notna() & lng.notna()

    # ── freeway distance, from the cached road outlines ──
    freeway = pd.Series([float("nan")] * n, index=idx, dtype=float)
    freeway_note = "no coordinates in this export"
    over = None
    if have_xy.any():
        pad = 0.3   # the same padding report.build_report uses, so both share one cache entry
        bbox = (float(lng[have_xy].min()) - pad, float(lat[have_xy].min()) - pad,
                float(lng[have_xy].max()) + pad, float(lat[have_xy].max()) + pad)
        pts, freeway_note = primary_road_points(bbox)
        if pts:
            import numpy as np
            plng = np.array([p[0] for p in pts]); plat = np.array([p[1] for p in pts])
            rlat = np.radians(plat); rlng = np.radians(plng)
            # A listing near the edge may have its true nearest freeway outside
            # the extent, so a distance greater than the padding is only a floor.
            over = pad * 69.0 * 0.9
            for i in idx[have_xy]:
                la, lo = math.radians(lat[i]), math.radians(lng[i])
                a = np.sin((rlat - la) / 2) ** 2 + math.cos(la) * np.cos(rlat) * np.sin((rlng - lo) / 2) ** 2
                freeway[i] = float(2 * 3958.8 * np.arcsin(np.sqrt(a.min())))
            status["used"].append("freeway distance (Census TIGER primary roads, cached map extent)")
        else:
            status["unavailable"].append(f"freeway distance: {freeway_note}")
    else:
        status["unavailable"].append(f"freeway distance: {freeway_note}")

    # ── airport distance, from the bundled table ──
    airport_mi = pd.Series([float("nan")] * n, index=idx, dtype=float)
    airport_name = pd.Series([""] * n, index=idx, dtype=object)
    table = airports() if have_xy.any() else []
    if table:
        for i in idx[have_xy]:
            best = min(table, key=lambda a: _miles(lat[i], lng[i], a["lat"], a["lng"]))
            airport_mi[i] = _miles(lat[i], lng[i], best["lat"], best["lng"])
            airport_name[i] = f"{best['name']} ({best['ident']})"
        status["used"].append(f"airport distance ({len(table)} US airports with scheduled service)")
    else:
        status["unavailable"].append("airport distance: " + ("no coordinates in this export" if not have_xy.any() else "airport table missing"))

    # ── county population growth and permits ──
    pop_growth = pd.Series([float("nan")] * n, index=idx, dtype=float)
    permits_1k = pd.Series([float("nan")] * n, index=idx, dtype=float)
    have_county = (state != "") & (county != "")
    pop_span = ""
    permit_year = 0
    if have_county.any():
        pops, pop_span, pop_note = county_population()
        perms, permit_year, perm_note = county_permits()
        # name lookup: (state fips, county key) -> county fips
        by_name = {}
        for (st, cty), (name, p0, p1) in pops.items():
            by_name.setdefault((st, _county_key(name)), cty)
        matched = 0
        for i in idx[have_county]:
            st = STATES[state[i]][0]
            cty = by_name.get((st, county[i]))
            if cty is None:
                continue
            matched += 1
            name, p0, p1 = pops[(st, cty)]
            if p0 > 0:
                pop_growth[i] = round((p1 - p0) / p0 * 100, 1)
            units = perms.get((st, cty))
            if units is not None and p1 > 0:
                permits_1k[i] = round(units / p1 * 1000, 1)
        if pops:
            status["used"].append(f"county population change {pop_span} (Census Population Estimates"
                                  + (f", {pop_note}" if pop_note else "") + f"); matched {matched} of {int(have_county.sum())} rows")
        else:
            status["unavailable"].append(f"county population: {pop_note}")
        if perms:
            status["used"].append(f"building permits {permit_year} (Census Building Permits Survey"
                                  + (f", {perm_note}" if perm_note else "") + ")")
        else:
            status["unavailable"].append(f"building permits: {perm_note}")
    else:
        status["unavailable"].append("county population and permits: no state or county in this export")

    # ── percentiles within the file, then the mean of what was available ──
    parts = pd.DataFrame({
        "freeway": _pct(freeway, higher_is_better=False),
        "airport": _pct(airport_mi, higher_is_better=False),
        "pop": _pct(pop_growth, True),
        "permits": _pct(permits_1k, True),
    }, index=idx)
    score = parts.mean(axis=1, skipna=True).fillna(50.0).round(1)

    def basis(i) -> str:
        bits = []
        if pd.notna(freeway[i]):
            bits.append((f"over {over:.0f} mi" if over and freeway[i] > over else f"{freeway[i]:.1f} mi") + " to a freeway")
        if pd.notna(airport_mi[i]):
            bits.append(f"{airport_mi[i]:.0f} mi to {airport_name[i]}")
        if pd.notna(pop_growth[i]):
            bits.append(f"county pop {pop_growth[i]:+.1f}% {pop_span}")
        if pd.notna(permits_1k[i]):
            bits.append(f"{permits_1k[i]:.1f} homes permitted per 1k residents ({permit_year})")
        if not bits:
            return "no growth signal available for this listing — scored neutral"
        missing = []
        if pd.isna(freeway[i]): missing.append("freeway")
        if pd.isna(pop_growth[i]) and have_county.get(i, False): missing.append("county not matched")
        return "; ".join(bits) + (f" (not measured: {', '.join(missing)})" if missing else "") + \
            "; interchanges and school quality are not measured"

    out = pd.concat([df, pd.DataFrame({
        "Freeway_Mi": freeway.round(1), "Airport_Mi": airport_mi.round(1),
        "Nearest_Airport": airport_name,
        "County_Pop_Growth_Pct": pop_growth, "County_Permits_Per_1k": permits_1k,
        "Growth_Basis": [basis(i) for i in idx],
        "_growth_score": score,
    }, index=idx)], axis=1)
    out.attrs["growth_status"] = status
    return out
