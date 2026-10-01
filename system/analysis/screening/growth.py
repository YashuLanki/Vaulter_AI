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

  * Freeway access -- miles to the nearest freeway EXIT (OpenStreetMap
    "motorway_junction", added 2026-09-29), falling back to miles to the
    nearest PRIMARY road (interstates and
    other limited-access highways) in the Census TIGER road layer. Reuses the
    road outlines the HTML report already fetches and caches per half-degree
    grid cell, so this adds NO network calls of its own to a screen. Reported
    honestly as "within the map extent": a listing whose nearest freeway lies
    outside the export's own bounding box reads as "over N miles", never as a
    made-up number. The exit replaced the road line as the measure because
    access is an exit: a parcel beside a freeway with no exit for miles is not
    connected.
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

# How long one screen may spend DOWNLOADING growth data (2026-09-29). Cached
# data is instant, so this only bites the first screen of a new market: six
# new states measured 258 s cold, long enough for Claude Desktop to give up on
# the call. Past the budget, remaining downloads are skipped and those signals
# are reported as not available THIS time; they fill in on a later screen as
# the cache warms. A judgement, set well under the connector's patience.
GROWTH_FETCH_BUDGET_SECONDS = 60
_deadline = [float("inf")]


def _out_of_time() -> bool:
    return time.monotonic() > _deadline[0]

# Distances past these are reported as "over", never as a measured figure: the
# exit search reaches ~16 mi beyond a listing's own map square, and the airport
# table is the 505 US airports with scheduled flights (Rural Nevada measured
# "188 mi to an exit" and Guam "3,405 mi to an airport" before these caps).
AIRPORT_CAP_MI = 150.0
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


QCEW_URL = "https://data.bls.gov/cew/data/api/{year}/a/industry/10.csv"
FHFA_URL = "https://www.fhfa.gov/hpi/download/annual/hpi_at_county.csv"   # an Excel file, despite the name
CHANGE_YEARS = 5
_UA = {"User-Agent": "Mozilla/5.0 (Vaulter AI screener)"}   # BLS refuses requests with no browser name


def _cached_bytes(name: str, url: str) -> tuple[bytes | None, str]:
    """A binary download through the shared cache; same freshness rules as the text files."""
    cache = _cache_dir()
    path = cache / name if cache else None
    stale = None
    if path is not None and path.exists():
        try:
            data = path.read_bytes()
            if (time.time() - path.stat().st_mtime) / 86400 <= GROWTH_CACHE_DAYS:
                return data, ""
            stale = data
        except OSError:
            pass
    if _out_of_time():
        return stale, ("from a cached copy more than %d days old" % GROWTH_CACHE_DAYS) if stale else "skipped, out of download time"
    try:
        import requests
        r = requests.get(url, timeout=_TIMEOUT * 2, headers=_UA)
        if r.status_code == 200 and r.content:
            if path is not None:
                try:
                    path.write_bytes(r.content)
                except OSError:
                    pass
            return r.content, ""
    except Exception as e:
        log.warning(f"Could not download {url}: {type(e).__name__}")
    if stale is not None:
        return stale, f"from a cached copy more than {GROWTH_CACHE_DAYS} days old"
    return None, "unreachable and not cached"


def county_jobs() -> tuple[dict, str, str]:
    """
    {(state fips, county fips): percent change in total jobs over CHANGE_YEARS},
    the span, and a caveat. From the BLS Quarterly Census of Employment and
    Wages annual averages -- every US county, keyless ("A key is not required
    for the QCEW API", BLS's own page), public domain.
    """
    def year_file(y):
        data, note = _cached_bytes(f"growth_qcew_{y}.csv", QCEW_URL.format(year=y))
        if not data:
            return None, note
        out = {}
        for row in csv.DictReader(io.StringIO(data.decode("latin-1"))):
            a = row.get("area_fips", "")
            if row.get("own_code") == "0" and len(a) == 5 and a.isdigit() and not a.endswith("000"):
                try:
                    out[(a[:2], a[2:])] = float(row["annual_avg_emplvl"])
                except (KeyError, ValueError):
                    pass
        return (out or None), note
    this_year = datetime.now().year
    for latest in range(this_year - 1, this_year - 4, -1):
        now, note = year_file(latest)
        if now:
            then, note2 = year_file(latest - CHANGE_YEARS)
            if not then:
                return {}, "", f"jobs {latest - CHANGE_YEARS}: {note2}"
            chg = {k: round((v - then[k]) / then[k] * 100, 1) for k, v in now.items() if then.get(k)}
            return chg, f"{latest - CHANGE_YEARS}-{latest}", note or note2
    return {}, "", "no recent jobs file could be read"


def county_prices() -> tuple[dict, str, str]:
    """
    {(state fips, county fips): percent change in the FHFA house price index
    over CHANGE_YEARS}, the span, and a caveat. FHFA's annual county index
    (all transactions); public domain. Counties with too few sales have no
    index, and those abstain.
    """
    data, note = _cached_bytes("growth_fhfa_county.xlsx", FHFA_URL)
    if not data:
        return {}, "", note
    try:
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True).active
        series = {}
        for row in ws.iter_rows(values_only=True):
            if not row or len(row) < 6 or not isinstance(row[3], (int, float)):
                continue
            f = str(row[2] or "").zfill(5)
            if isinstance(row[5], (int, float)):
                series.setdefault((f[:2], f[2:]), {})[int(row[3])] = float(row[5])
    except Exception as e:
        return {}, "", f"house price file unreadable ({type(e).__name__})"
    if not series:
        return {}, "", "house price file held no county rows"
    latest = max(max(v) for v in series.values())
    out = {k: round((v[latest] - v[latest - CHANGE_YEARS]) / v[latest - CHANGE_YEARS] * 100, 1)
           for k, v in series.items() if latest in v and v.get(latest - CHANGE_YEARS)}
    return out, f"{latest - CHANGE_YEARS}-{latest}", note


SCHOOLS_URL = "https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/"
SCHOOL_RADIUS_MI = 5.0
SCHOOL_BASELINE_YEARS = 5
SCHOOL_MIN_PUPILS = 300     # below this, a percentage change is noise, so it abstains


def school_directory(state_fips: str, year: int) -> tuple[dict | None, str]:
    """
    {school id: (lat, lng, pupils)} for every public school in one state and
    school year, from the federal Common Core of Data served keyless by the
    Urban Institute's Education Data Portal (licence: attribution, commercial
    use allowed). Sends only a state number. Cached in the shared geo cache for
    GROWTH_CACHE_DAYS like the Census files; None when it cannot be had.
    """
    import json as _json
    cache = _cache_dir()
    path = cache / f"growth_schools_{state_fips}_{year}.json" if cache else None
    if path is not None and path.exists():
        try:
            if (time.time() - path.stat().st_mtime) / 86400 <= GROWTH_CACHE_DAYS:
                raw = _json.loads(path.read_text(encoding="utf-8"))
                return {k: tuple(v) for k, v in raw.items()}, ""
        except (OSError, ValueError):
            pass
    try:
        import requests
        url, params, out = SCHOOLS_URL.format(year=year), {"fips": int(state_fips)}, {}
        while url:
            r = requests.get(url, params=params, timeout=_TIMEOUT * 2)
            if r.status_code != 200:
                return None, f"school directory {year}: HTTP {r.status_code}"
            j = r.json()
            for x in j.get("results", []):
                la, lo, n = x.get("latitude"), x.get("longitude"), x.get("enrollment")
                if la is None or lo is None or n is None or n < 0 or x.get("school_status") not in (1, 3, 4, 5, 8):
                    continue   # negative = "missing" codes; status 2 closed, 6 inactive, 7 future
                out[str(x.get("ncessch"))] = (float(la), float(lo), int(n))
            url, params = j.get("next"), None
    except Exception as e:
        return None, f"school directory {year}: {type(e).__name__}"
    if not out:
        return None, f"school directory {year}: no schools returned"
    if path is not None:
        try:
            path.write_text(_json.dumps(out), encoding="utf-8")
        except OSError:
            pass
    return out, ""


def _schools_cached(state_fips: str) -> bool:
    """True when this state's recent school years are already in the shared cache."""
    cache = _cache_dir()
    if cache is None:
        return False
    return len(list(cache.glob(f"growth_schools_{state_fips}_*.json"))) >= 2


def school_years(state_fips: str) -> tuple[dict | None, dict | None, str]:
    """(latest, baseline five years earlier, caveat) -- the newest year that answers."""
    this_year = datetime.now().year
    for latest in range(this_year - 1, this_year - 5, -1):
        now, note = school_directory(state_fips, latest)
        if now:
            then, note2 = school_directory(state_fips, latest - SCHOOL_BASELINE_YEARS)
            return now, then, (f"{latest - SCHOOL_BASELINE_YEARS}-{latest}" if then else note2)
    return None, None, "no recent school year could be read"


def interchange_points(lats, lngs) -> tuple[list, str, set]:
    """
    Every freeway exit (OpenStreetMap "motorway_junction") near the listings,
    as (lat, lng, exit number), plus a caveat and the set of one-degree cells
    that could NOT be fetched.

    Added 2026-09-29 to close "major interchanges" in the teammate's growth
    list: access is an exit, not a freeway line -- a parcel beside a freeway
    with no exit for ten miles is not connected. National, keyless, and the
    same Overpass mirrors proximity_tool already uses, with their coverage
    checks and their 7-day shared cache.

    Queried in one-degree cells (plus a quarter-degree margin), only for cells
    that hold a listing, so a statewide export stays a handful of queries and
    two exports over the same area share cached cells. A cell that cannot be
    fetched is reported, and its listings fall back to the freeway line.
    """
    import math as _m
    try:
        from analysis.screening.geo_providers import _overpass, _overpass_cache_read
    except Exception as e:
        return [], f"exit data unavailable ({type(e).__name__})", set()
    cells = {(_m.floor(la), _m.floor(lo)) for la, lo in zip(lats, lngs)
             if la == la and lo == lo}
    seen, pts, failed = set(), [], set()
    for la, lo in sorted(cells):
        q = (f'[out:json][timeout:60];node["highway"="motorway_junction"]'
             f'({la - 0.25:.2f},{lo - 0.25:.2f},{la + 1.25:.2f},{lo + 1.25:.2f});out;')
        hit = _overpass_cache_read(q)          # cached squares are free: never skipped
        if hit is not None:
            data = hit[0]
        elif _out_of_time():
            failed.add((la, lo))
            continue
        else:
            data = _overpass(q, empty_is_suspect=False)
        if data is None:
            failed.add((la, lo))
            continue
        for el in data.get("elements", []):
            if el.get("id") in seen or "lat" not in el:
                continue
            seen.add(el["id"])
            pts.append((float(el["lat"]), float(el["lon"]), str((el.get("tags") or {}).get("ref", ""))))
    note = ""
    if failed:
        note = f"exit data could not be fetched for {len(failed)} of {len(cells)} map square(s)"
    return pts, note, failed


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
    _deadline[0] = time.monotonic() + GROWTH_FETCH_BUDGET_SECONDS
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
        # One map for a file spanning several states is a request the Census
        # road service retries for minutes and then refuses (measured: 191 s,
        # then "unavailable", for six states). Past one region the exits,
        # which are fetched square by square, carry freeway access alone.
        if bbox[2] - bbox[0] > 3.0 or bbox[3] - bbox[1] > 3.0:
            pts, freeway_note = [], ("this export spans more than one region, so the freeway line "
                                     "was not fetched; freeway exits carry access")
        else:
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

    # ── nearest freeway exit, from OpenStreetMap; the freeway line is the fallback ──
    exit_mi = pd.Series([float("nan")] * n, index=idx, dtype=float)
    exit_ref = pd.Series([""] * n, index=idx, dtype=object)
    exit_cap = 0.25 * 69.0 * 0.9      # beyond the margin, the true nearest may be out of range
    if have_xy.any():
        import math as _m
        import numpy as np
        epts, enote, failed = interchange_points(lat[have_xy], lng[have_xy])
        if epts:
            elat = np.radians([p[0] for p in epts]); elng = np.radians([p[1] for p in epts])
            for i in idx[have_xy]:
                if (_m.floor(lat[i]), _m.floor(lng[i])) in failed:
                    continue
                la, lo = math.radians(lat[i]), math.radians(lng[i])
                a = np.sin((elat - la) / 2) ** 2 + math.cos(la) * np.cos(elat) * np.sin((elng - lo) / 2) ** 2
                k = int(a.argmin())
                exit_mi[i] = min(float(2 * 3958.8 * np.arcsin(np.sqrt(a[k]))), exit_cap)
                exit_ref[i] = epts[k][2] if exit_mi[i] < exit_cap else ""
            status["used"].append(f"nearest freeway exit ({len(epts):,} exits, OpenStreetMap)"
                                  + (f"; {enote}, those listings use the freeway line" if enote else ""))
        else:
            status["unavailable"].append(f"freeway exits: {enote or 'none found near these listings'}"
                                         " -- the freeway line is used instead")
    # Access = distance to the nearest exit where known, else to the freeway line.
    # OpenStreetMap marks exits on expressways and state routes too, which the
    # Census primary-road layer leaves out -- so the nearest exit is often
    # CLOSER than the nearest "freeway" line (median 0.9 vs 2.0 mi on one real
    # export). That is the better measure of access, and the label says so.
    access = exit_mi.where(exit_mi.notna(), freeway)

    # ── airport distance, from the bundled table ──
    airport_mi = pd.Series([float("nan")] * n, index=idx, dtype=float)
    airport_name = pd.Series([""] * n, index=idx, dtype=object)
    table = airports() if have_xy.any() else []
    if table:
        for i in idx[have_xy]:
            best = min(table, key=lambda a: _miles(lat[i], lng[i], a["lat"], a["lng"]))
            airport_mi[i] = min(_miles(lat[i], lng[i], best["lat"], best["lng"]), AIRPORT_CAP_MI)
            airport_name[i] = f"{best['name']} ({best['ident']})" if airport_mi[i] < AIRPORT_CAP_MI else ""
        status["used"].append(f"airport distance ({len(table)} US airports with scheduled service)")
    else:
        status["unavailable"].append("airport distance: " + ("no coordinates in this export" if not have_xy.any() else "airport table missing"))

    # ── county population growth and permits ──
    pop_growth = pd.Series([float("nan")] * n, index=idx, dtype=float)
    permits_1k = pd.Series([float("nan")] * n, index=idx, dtype=float)
    fips = pd.Series([None] * n, index=idx, dtype=object)
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
            fips[i] = (st, cty)
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

    # ── county jobs and house prices, five-year change (national, by county) ──
    jobs_chg = pd.Series([float("nan")] * n, index=idx, dtype=float)
    price_chg = pd.Series([float("nan")] * n, index=idx, dtype=float)
    jobs_span = price_span = ""
    if fips.notna().any():
        jobs, jobs_span, jnote = county_jobs()
        prices, price_span, pnote = county_prices()
        for i in idx[fips.notna()]:
            if fips[i] in jobs:
                jobs_chg[i] = jobs[fips[i]]
            if fips[i] in prices:
                price_chg[i] = prices[fips[i]]
        (status["used"] if jobs else status["unavailable"]).append(
            f"county jobs change {jobs_span} (BLS QCEW)" + (f", {jnote}" if jnote and jobs else "") if jobs
            else f"county jobs: {jnote}")
        (status["used"] if prices else status["unavailable"]).append(
            f"county house-price change {price_span} (FHFA)" + (f", {pnote}" if pnote and prices else "") if prices
            else f"county house prices: {pnote}")

    # ── public-school enrolment within 5 miles, now against five years earlier ──
    # A national stand-in for "is the area filling with families": no free,
    # national, commercially-usable measure of school QUALITY exists (state
    # grades differ state to state; the one national score forbids commercial
    # use), but every public school's location and pupil count does. A school
    # that opened in the window counts as growth -- schools follow homes.
    school_n = pd.Series([float("nan")] * n, index=idx, dtype=float)
    school_chg = pd.Series([float("nan")] * n, index=idx, dtype=float)
    school_span = ""
    if have_xy.any():
        import numpy as np
        dirs, notes = {}, []
        for st in sorted({state[i] for i in idx[have_xy] if state[i]}):
            if _out_of_time() and not _schools_cached(STATES[st][0]):
                notes.append(f"{st}: skipped, out of download time this screen (fills in next time)")
                continue
            now, then, span = school_years(STATES[st][0])
            if now and then:
                dirs[st] = (now, then); school_span = span
            else:
                notes.append(f"{st}: {span}")
        for i in idx[have_xy]:
            if state[i] not in dirs:
                continue
            now, then = dirs[state[i]]
            def within(d):
                arr = np.array(list(d.values()), dtype=float)
                la, lo = math.radians(lat[i]), math.radians(lng[i])
                rl, rg = np.radians(arr[:, 0]), np.radians(arr[:, 1])
                a = np.sin((rl - la) / 2) ** 2 + math.cos(la) * np.cos(rl) * np.sin((rg - lo) / 2) ** 2
                near = 2 * 3958.8 * np.arcsin(np.sqrt(a)) <= SCHOOL_RADIUS_MI
                return int(near.sum()), float(arr[near, 2].sum())
            c1, p1 = within(now)
            c0, p0 = within(then)
            school_n[i] = c1
            if p0 >= SCHOOL_MIN_PUPILS:
                school_chg[i] = round((p1 - p0) / p0 * 100, 1)
        if dirs:
            status["used"].append(f"public-school pupils within {SCHOOL_RADIUS_MI:.0f} mi, {school_span} "
                                  f"(federal Common Core of Data via the Urban Institute)"
                                  + (f"; not available for {'; '.join(notes)}" if notes else ""))
        else:
            status["unavailable"].append("school enrolment: " + ("; ".join(notes) or "no state in this export"))

    # ── percentiles within the file, then the mean of what was available ──
    parts = pd.DataFrame({
        "access": _pct(access, higher_is_better=False),
        "airport": _pct(airport_mi, higher_is_better=False),
        "pop": _pct(pop_growth, True),
        "permits": _pct(permits_1k, True),
        "schools": _pct(school_chg, True),
        "jobs": _pct(jobs_chg, True),
        "prices": _pct(price_chg, True),
    }, index=idx)
    # One voice per KIND of evidence, not per signal (2026-09-29). Four of the
    # seven signals are county-level and move together -- a hot county's
    # population, permits, jobs and prices all rise at once -- so a plain mean
    # gave the county four votes to the local schools' one, and a listing's
    # own surroundings barely registered. Average within each group, then
    # across the groups that have anything to say.
    groups = pd.DataFrame({
        "connectivity": parts[["access", "airport"]].mean(axis=1, skipna=True),
        "county momentum": parts[["pop", "permits", "jobs", "prices"]].mean(axis=1, skipna=True),
        "local": parts["schools"],
    }, index=idx)
    score = groups.mean(axis=1, skipna=True).fillna(50.0).round(1)

    def basis(i) -> str:
        bits = []
        if pd.notna(exit_mi[i]):
            ref = f" (exit {exit_ref[i]})" if exit_ref[i] else ""
            bits.append((f"over {exit_cap:.0f} mi" if exit_mi[i] >= exit_cap else f"{exit_mi[i]:.1f} mi")
                        + f" to a freeway or expressway exit{ref}")
        elif pd.notna(freeway[i]):
            bits.append((f"over {over:.0f} mi" if over and freeway[i] > over else f"{freeway[i]:.1f} mi")
                        + " to a freeway (exit data unavailable)")
        if pd.notna(airport_mi[i]):
            bits.append(f"over {AIRPORT_CAP_MI:.0f} mi to an airport with scheduled flights"
                        if airport_mi[i] >= AIRPORT_CAP_MI else f"{airport_mi[i]:.0f} mi to {airport_name[i]}")
        if pd.notna(pop_growth[i]):
            bits.append(f"county pop {pop_growth[i]:+.1f}% {pop_span}")
        if pd.notna(permits_1k[i]):
            bits.append(f"{permits_1k[i]:.1f} homes permitted per 1k residents ({permit_year})")
        if pd.notna(jobs_chg[i]):
            low = " from a pandemic low" if jobs_span.startswith("2020") else ""
            bits.append(f"county jobs {jobs_chg[i]:+.0f}% {jobs_span}{low}")
        if pd.notna(price_chg[i]):
            bits.append(f"county house prices {price_chg[i]:+.0f}% {price_span}")
        if pd.notna(school_chg[i]):
            bits.append(f"public-school pupils within {SCHOOL_RADIUS_MI:.0f} mi {school_chg[i]:+.0f}% {school_span}")
        elif pd.notna(school_n[i]):
            bits.append(f"too few public-school pupils within {SCHOOL_RADIUS_MI:.0f} mi to measure a trend")
        if not bits:
            return "no growth signal available for this listing — scored neutral"
        missing = []
        if pd.isna(access[i]): missing.append("freeway access")
        if pd.isna(pop_growth[i]) and have_county.get(i, False): missing.append("county not matched")
        return "; ".join(bits) + (f" (not measured: {', '.join(missing)})" if missing else "") + \
            "; school quality (as opposed to enrolment) and proposed developments are not measured"

    out = pd.concat([df, pd.DataFrame({
        "Freeway_Exit_Mi": exit_mi.round(1), "Freeway_Exit": exit_ref,
        "Freeway_Mi": freeway.round(1), "Airport_Mi": airport_mi.round(1),
        "Nearest_Airport": airport_name,
        "County_Pop_Growth_Pct": pop_growth, "County_Permits_Per_1k": permits_1k,
        "Schools_5mi": school_n, "School_Pupils_Change_Pct": school_chg,
        "County_Jobs_Change_Pct": jobs_chg, "County_House_Price_Change_Pct": price_chg,
        "Growth_Basis": [basis(i) for i in idx],
        "_growth_score": score,
    }, index=idx)], axis=1)
    out.attrs["growth_status"] = status
    return out
