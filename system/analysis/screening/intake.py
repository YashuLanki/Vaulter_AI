"""
Make any listing export screenable: CoStar, Crexi, LoopNet, a broker's spreadsheet.

Built 2026-09-29 for the goal "drop any export in the CoStar Drop folder, ask
Claude to screen it, get back what Vaulter should pursue". normalise_columns()
already finds each concept by name and by what its values look like. What it
could not do, and what every non-CoStar source needs, is this step:

  * NUMBERS WRITTEN AS TEXT. Scraped and broker files write prices with a
    dollar sign and commas,
    "1.2M", "12.5 AC", "43,560 SF", "Call for pricing". The screener's number
    reader took plain digits only, so for such a file price and size -- the two
    things the 3x test rests on -- were silently blank.
  * DAYS ON MARKET from a listing date ("Listed On", "Date Listed").
  * CITY, STATE, ZIP from one combined address line.
  * COORDINATES from an address, when a file has none -- through the Census
    Bureau's free batch geocoder. That sends the listings' PUBLIC addresses and
    nothing else: never a firm name, deal, price or owner. Without it, most of
    the growth score (every distance, every county figure) was switched off.
  * COUNTY from coordinates, using the county outlines the report already
    fetches and caches -- no new service.

Every repair is REPORTED in the same provenance list the screen already prints
("price: parsed from text on 212 rows"), so a reader can see what was recovered
and how. Nothing is guessed: a value that cannot be read stays blank, and a
blank scores neutral as everywhere else. On a clean CoStar export this step
changes nothing.
"""
from __future__ import annotations

import io
import logging
import math
import re
import time
from datetime import datetime

import pandas as pd

log = logging.getLogger("vaulter.intake")

SQFT_PER_ACRE = 43560.0
_NOT_A_PRICE = re.compile(r"call|unpriced|tbd|n/?a|negotiable|upon request|contact|auction|submit|offer",
                          re.I)
_MULT = {"k": 1e3, "m": 1e6, "mm": 1e6, "b": 1e9, "bn": 1e9}
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
GEOCODE_MAX_ROWS = 2000       # one batch; beyond this the Census asks for several
GEOCODE_TIMEOUT = 90


# ─── Numbers ──────────────────────────────────────────────────────────────────

def money(v) -> float:
    """A price with a dollar sign and commas, '1.25M', a 'K' suffix, or a plain number -> float;
    'Call for pricing' -> NaN."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s or _NOT_A_PRICE.search(s):
        return float("nan")
    s = s.replace("$", "").replace(",", "").replace("USD", "").strip()
    # A range ("1.2M - 1.5M") -> its low end: the asking floor, never an invented middle.
    s = re.split(r"\s*(?:-|–|to)\s*", s)[0]
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)\s*([a-zA-Z]{0,2})", s)
    if not m:
        return float("nan")
    num, suf = float(m.group(1)), m.group(2).lower()
    if suf and suf not in _MULT:
        return float("nan")
    return num * _MULT.get(suf, 1.0)


def acres(v, column_is_sf: bool = False) -> float:
    """'12.5 AC' / '12.5 acres' / '544,500 SF' / '±73.55' -> acres."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return float("nan")
    if isinstance(v, (int, float)):
        return float(v) / SQFT_PER_ACRE if column_is_sf else float(v)
    s = str(v).lower().replace(",", "").replace("±", "").replace("~", "").strip()
    m = re.search(r"([0-9]*\.?[0-9]+)", s)
    if not m:
        return float("nan")
    num = float(m.group(1))
    if re.search(r"\b(sf|sq\.?\s*ft|square\s*f(ee|oo)t)", s):
        return num / SQFT_PER_ACRE
    if re.search(r"\bac(res?)?\b|\bacre", s):
        return num
    return num / SQFT_PER_ACRE if column_is_sf else num


def _is_text(series: pd.Series) -> bool:
    """Holds numbers written as text. NOT a dtype == object test: pandas 3 stores
    text as its own string type, and that test silently skipped every repair on
    this machine (caught 2026-09-29 -- every Crexi-shaped price stayed text and
    every listing scored "untestable"). Same trap requirements.txt records."""
    if pd.api.types.is_numeric_dtype(series):
        return False
    return series.dropna().astype(str).str.contains(r"[^\d.\-]", regex=True).any()


# ─── The repairs ──────────────────────────────────────────────────────────────

def repair(df: pd.DataFrame, report: list[dict]) -> pd.DataFrame:
    """Apply every repair in place-safe order; update `report`; never raise."""
    for step in (_numbers, _clean_types, _days_on_market, _split_address, _geocode,
                 _county_from_points, _county_from_zip, _county_names):
        try:
            df = step(df, report)
        except Exception as e:            # a repair must never cost the screen
            log.warning(f"[INTAKE] {step.__name__} skipped: {type(e).__name__}: {e}")
    return df


def _entry(report, field):
    e = next((r for r in report if r["field"] == field), None)
    if e is None:
        e = {"field": field, "source": "", "derived": "", "note": "", "rows": 0}
        report.append(e)
    return e


def _numbers(df, report):
    """Prices and sizes written as text become numbers; the report says so."""
    for field in ("For Sale Price", "Last Sale Price"):
        if field in df.columns and _is_text(df[field]):
            before = pd.to_numeric(df[field], errors="coerce").notna().sum()
            df[field] = df[field].map(money)
            e = _entry(report, field)
            e.update(rows=int(df[field].notna().sum()),
                     derived=(e.get("derived") or "") + " read from text ($, commas, M/K)".strip(),
                     note=(e.get("note") + "; " if e.get("note") else "") +
                          f"written as text -- read on {int(df[field].notna().sum())} rows "
                          f"(was {int(before)})")
    f = "Land Area (AC)"
    if f in df.columns and _is_text(df[f]):
        src = str(_entry(report, f).get("source") or "").lower()
        sf = bool(re.search(r"\bsf\b|sq|square", src))
        df[f] = df[f].map(lambda v: acres(v, sf))
        e = _entry(report, f)
        e.update(rows=int(df[f].notna().sum()), derived="read from text with its units",
                 note=(e.get("note") + "; " if e.get("note") else "") + "sizes written as text, units read per row")
    elif f in df.columns:
        # A plain-number size column with no unit in its name: square feet if
        # the typical value is far beyond any acreage the firm has evaluated.
        vals = pd.to_numeric(df[f], errors="coerce")
        src = str(_entry(report, f).get("source") or "").lower()
        if vals.notna().any() and vals.median() > 20000 and not re.search(r"\bac|acre", src):
            df[f] = vals / SQFT_PER_ACRE
            _entry(report, f).update(derived="converted from square feet",
                                     note=f"'{src}' holds square feet (typical value {vals.median():,.0f}); converted")
    for f in ("Latitude", "Longitude", "Days On Market"):
        if f in df.columns and _is_text(df[f]):
            df[f] = pd.to_numeric(df[f].astype(str).str.replace(",", "").str.extract(r"(-?[0-9]*\.?[0-9]+)")[0],
                                  errors="coerce")
    return df


_DATE_COL = re.compile(r"(list(ed|ing)?\s*(on|date)|date\s*listed|on\s*market\s*(since|date)|first\s*listed)", re.I)


def _days_on_market(df, report):
    """Days on market from a listing date, where no days column exists."""
    if "Days On Market" in df.columns and pd.to_numeric(df["Days On Market"], errors="coerce").notna().any():
        return df
    col = next((c for c in df.columns if _DATE_COL.search(str(c))), None)
    if col is None:
        return df
    when = pd.to_datetime(df[col], errors="coerce")
    if when.notna().sum() == 0:
        return df
    today = pd.Timestamp(datetime.now().date())
    dom = (today - when).dt.days
    df["Days On Market"] = dom.where(dom >= 0)
    _entry(report, "Days On Market").update(source=str(col), derived="worked out from the listing date",
                                            note=f"from '{col}'", rows=int(df["Days On Market"].notna().sum()))
    return df


_ADDR = re.compile(r"^\s*(?P<street>.*?),\s*(?P<city>[A-Za-z .'\-]+),\s*(?P<state>[A-Za-z]{2})\b\.?\s*(?P<zip>\d{5})?", re.I)


def _split_address(df, report):
    """'123 Main St, Casa Grande, AZ 85122' -> City, State, Zip where those are missing."""
    if "Property Address" not in df.columns:
        return df
    need_city = "City" not in df.columns or df["City"].isna().all()
    need_state = "State" not in df.columns or df["State"].isna().all()
    if not (need_city or need_state):
        return df
    parts = df["Property Address"].astype(str).str.extract(_ADDR)
    got = parts["state"].notna().sum()
    if not got:
        return df
    if need_city:
        df["City"] = parts["city"].str.strip()
        _entry(report, "City").update(source="Property Address", derived="split out of the address",
                                      rows=int(df["City"].notna().sum()), note="split out of the address")
    if need_state:
        df["State"] = parts["state"].str.upper()
        _entry(report, "State").update(source="Property Address", derived="split out of the address",
                                       rows=int(df["State"].notna().sum()), note="split out of the address")
    if "Zip" not in df.columns:
        df["Zip"] = parts["zip"]
    return df


def _geocode(df, report):
    """Coordinates for rows that have an address but none, via the Census batch geocoder."""
    have = pd.Series(False, index=df.index)
    if "Latitude" in df.columns and "Longitude" in df.columns:
        have = pd.to_numeric(df["Latitude"], errors="coerce").notna() & \
               pd.to_numeric(df["Longitude"], errors="coerce").notna()
    if have.all() or "Property Address" not in df.columns:
        return df
    todo = df.index[~have & df["Property Address"].notna()]
    if len(todo) == 0:
        return df
    todo = todo[:GEOCODE_MAX_ROWS]
    street = df.loc[todo, "Property Address"].astype(str).str.split(",").str[0].str.strip()
    city = df.loc[todo, "City"].astype(str) if "City" in df.columns else pd.Series("", index=todo)
    state = df.loc[todo, "State"].astype(str) if "State" in df.columns else pd.Series("", index=todo)
    zipc = df.loc[todo, "Zip"].astype(str) if "Zip" in df.columns else pd.Series("", index=todo)
    buf = io.StringIO()
    for i in todo:
        clean = lambda x: "" if str(x).lower() in ("nan", "none") else str(x).replace(",", " ")
        buf.write(f"{i},{clean(street[i])},{clean(city[i])},{clean(state[i])},{clean(zipc[i])}\n")
    try:
        import requests
        t0 = time.time()
        r = requests.post(GEOCODER_URL, files={"addressFile": ("a.csv", buf.getvalue())},
                          data={"benchmark": "Public_AR_Current"}, timeout=GEOCODE_TIMEOUT)
        if r.status_code != 200:
            _entry(report, "Latitude").update(note=f"address lookup unavailable (HTTP {r.status_code})")
            return df
    except Exception as e:
        _entry(report, "Latitude").update(note=f"address lookup unavailable ({type(e).__name__})")
        return df
    import csv as _csv
    found = 0
    if "Latitude" not in df.columns:
        df["Latitude"] = float("nan")
    if "Longitude" not in df.columns:
        df["Longitude"] = float("nan")
    for row in _csv.reader(io.StringIO(r.text)):
        # id, input, Match/No_Match, Exact/Non_Exact, matched address, "lon,lat", tigerline, side
        if len(row) >= 6 and row[2] == "Match":
            try:
                lon, lat = (float(x) for x in row[5].split(","))
                idx = type(df.index[0])(row[0]) if len(df.index) else row[0]
                df.at[idx, "Latitude"], df.at[idx, "Longitude"] = lat, lon
                found += 1
            except (ValueError, KeyError):
                continue
    for f in ("Latitude", "Longitude"):
        _entry(report, f).update(derived="looked up from the address",
                                 rows=int(pd.to_numeric(df[f], errors="coerce").notna().sum()),
                                 note=f"{found} of {len(todo)} addresses placed by the Census address "
                                      f"lookup ({time.time() - t0:.0f}s); only the public listing "
                                      f"addresses were sent")
    return df


def _county_from_points(df, report):
    """County from coordinates, with the county outlines the report already caches."""
    if "County Name" in df.columns and df["County Name"].notna().mean() > 0.9:
        return df
    if "Latitude" not in df.columns or "Longitude" not in df.columns:
        return df
    lat = pd.to_numeric(df["Latitude"], errors="coerce")
    lng = pd.to_numeric(df["Longitude"], errors="coerce")
    ok = lat.notna() & lng.notna()
    if not ok.any():
        return df
    need = ok & (df["County Name"].isna() if "County Name" in df.columns else True)
    if not need.any():
        return df
    from analysis.screening.report import build_basemap
    pad = 0.3
    bbox = (float(lng[ok].min()) - pad, float(lat[ok].min()) - pad,
            float(lng[ok].max()) + pad, float(lat[ok].max()) + pad)
    if bbox[2] - bbox[0] > 6 or bbox[3] - bbox[1] > 6:
        _entry(report, "County Name").update(note="county not worked out: this export spans too wide an area")
        return df
    rings = (build_basemap(bbox) or {}).get("counties") or []
    if not rings:
        return df

    def inside(x, y, ring):
        c, j = False, len(ring) - 1
        for k in range(len(ring)):
            xi, yi = ring[k]; xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
                c = not c
            j = k
        return c
    if "County Name" not in df.columns:
        df["County Name"] = None
    n = 0
    for i in df.index[need]:
        for shape in rings:
            if inside(lng[i], lat[i], shape["r"]):
                df.at[i, "County Name"] = shape["n"]; n += 1
                break
    _entry(report, "County Name").update(derived="worked out from the coordinates", source="coordinates",
                                         rows=int(df["County Name"].notna().sum()),
                                         note=f"{n} rows placed in a county from their coordinates")
    return df


def _clean_types(df, report):
    """
    "Residential Land" / "Land - Commercial" -> "Residential" / "Commercial".
    Crexi and LoopNet label a land listing's use with the word Land attached,
    and the screener prices by type: "Residential Land" was not recognised as
    residential, so it carried no entitlement cost and its exit comps came from
    the wrong peers (measured 2026-09-29: ranking agreement with the same
    listings' CoStar screen was 0.49 before this). CoStar's own values carry no
    such word, so they pass through unchanged.
    """
    f = "Secondary Type"
    if f not in df.columns:
        return df
    raw = df[f].astype(str)
    if not raw.str.contains(r"\bland\b", case=False, regex=True).any():
        return df
    cleaned = (raw.str.replace(r"\bland\b", "", case=False, regex=True)
                  .str.replace(r"^[\s\-/:,]+|[\s\-/:,]+$", "", regex=True)
                  .str.replace(r"\s{2,}", " ", regex=True).str.strip())
    cleaned = cleaned.where(~cleaned.str.lower().isin(["", "nan", "none", "unknown"]), "Unknown")
    changed = int((cleaned != raw).sum())
    df[f] = cleaned
    e = _entry(report, f)
    e.update(note=(e.get("note") + "; " if e.get("note") else "") + f"'Land' dropped from {changed} type labels")
    return df


ZCTA_COUNTY_URL = ("https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
                   "tab20_zcta520_county20_natl.txt")
_ZIP_COUNTY: dict = {}


def _zip_to_county() -> dict:
    """{zip: (state fips, county name)} -- the county holding most of each ZIP's land."""
    if _ZIP_COUNTY:
        return _ZIP_COUNTY
    from analysis.screening.growth import _cached_bytes
    data, _ = _cached_bytes("intake_zip_county.txt", ZCTA_COUNTY_URL)
    if not data:
        return {}
    best = {}
    for line in data.decode("utf-8-sig", errors="replace").splitlines()[1:]:
        p = line.split("|")
        if len(p) < 17 or not p[1] or not p[9]:
            continue
        try:
            land = float(p[16] or 0)
        except ValueError:
            continue
        if land > best.get(p[1], (0, "", ""))[0]:
            best[p[1]] = (land, p[9][:2], p[10])
    _ZIP_COUNTY.update({z: (st, name) for z, (_, st, name) in best.items()})
    return _ZIP_COUNTY


def _county_from_zip(df, report):
    """
    County from the ZIP code, for rows still without one -- typically land
    listings whose address is an intersection the Census address lookup cannot
    place (it placed 91 of 215 on a LoopNet-shaped file). The county is all the
    four county-level growth signals need; distances stay unmeasured for those
    rows and score neutral, as any missing value does.
    """
    if "County Name" in df.columns and df["County Name"].notna().all():
        return df
    zips = None
    if "Zip" in df.columns:
        zips = df["Zip"].astype(str).str.extract(r"(\d{5})")[0]
    if (zips is None or zips.isna().all()) and "Property Address" in df.columns:
        zips = df["Property Address"].astype(str).str.extract(r"\b(\d{5})(?:-\d{4})?\s*$")[0]
    if zips is None or zips.isna().all():
        return df
    table = _zip_to_county()
    if not table:
        _entry(report, "County Name").update(note="ZIP-to-county table unavailable")
        return df
    if "County Name" not in df.columns:
        df["County Name"] = None
    need = df["County Name"].isna() & zips.notna()
    n = 0
    for i in df.index[need]:
        hit = table.get(zips[i])
        if hit:
            df.at[i, "County Name"] = hit[1]; n += 1
    if n:
        e = _entry(report, "County Name")
        e.update(rows=int(df["County Name"].notna().sum()),
                 note=(e.get("note") + "; " if e.get("note") else "") + f"{n} more from the ZIP code")
    return df


def _county_names(df, report):
    """One spelling per county ("Maricopa County" and "Maricopa" -> "Maricopa"), as CoStar writes
    it: prices are compared within a county, so two spellings would split one market in two."""
    if "County Name" in df.columns:
        df["County Name"] = (df["County Name"].astype(object).where(df["County Name"].notna())
                             .map(lambda v: v if v is None or (isinstance(v, float) and math.isnan(v))
                                  else re.sub(r"\s+(county|parish|borough)$", "", str(v).strip(), flags=re.I)))
    return df
