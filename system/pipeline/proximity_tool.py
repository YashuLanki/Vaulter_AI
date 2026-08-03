"""
proximity_tool.py
-----------------
Vaulter AI — Proximity Search MCP Tool

Drop this file into the vaulter_ai/ root directory.
Reads all configuration from data/config.json — no hardcoding.
Reads properties from data/Vaulter_Project_Master.csv (or .xlsx/.pdf).

Called by the proximity_search MCP tool in mcp_server.py.
"""

import csv
import glob
import json
import logging
import math
import os
import re
import time
from pathlib import Path

log = logging.getLogger("vaulter.proximity")

# ── State normalization ────────────────────────────────────────────────────
STATE_ABBR = {
    "Arizona": "AZ", "California": "CA", "Colorado": "CO",
    "New Mexico": "NM", "Texas": "TX", "Alabama": "AL", "Alaska": "AK",
    "Arkansas": "AR", "Connecticut": "CT", "Delaware": "DE", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA",
    "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO",
    "Montana": "MT", "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}
VALID_ABBR = set(STATE_ABBR.values())


def _norm_state(raw: str) -> str:
    raw = raw.strip()
    if raw in VALID_ABBR:
        return raw
    if raw in STATE_ABBR:
        return STATE_ABBR[raw]
    for full, abbr in STATE_ABBR.items():
        if full.startswith(raw) or raw.startswith(full[:5]):
            return abbr
    return ""


# ── Config loader ──────────────────────────────────────────────────────────
def _load_config() -> tuple:
    """
    Load categories and settings directly from config.py.
    config.py is the central Vaulter AI config — no separate config.json needed.
    Returns (categories, settings).
    """
    try:
        from config import (
            PROXIMITY_CATEGORIES,
            PROXIMITY_DEFAULT_RADIUS_MILES,
            PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY,
            PROXIMITY_GEOCODING_TIMEOUT,
            PROXIMITY_PLACES_REQUEST_DELAY,
        )
        categories = PROXIMITY_CATEGORIES
        settings = {
            "default_radius_miles":         PROXIMITY_DEFAULT_RADIUS_MILES,
            "summary_results_per_category": PROXIMITY_SUMMARY_RESULTS_PER_CATEGORY,
            "geocoding_timeout_seconds":    PROXIMITY_GEOCODING_TIMEOUT,
            "places_request_delay_seconds": PROXIMITY_PLACES_REQUEST_DELAY,
        }
        return categories, settings
    except ImportError as e:
        raise ImportError(
            f"Missing proximity settings in config.py: {e}\n"
            f"Add PROXIMITY_CATEGORIES and settings to config.py."
        )


# ── Project Master loader ──────────────────────────────────────────────────
def _load_project_master(data_dir: Path) -> dict:
    """
    Returns {property_name: state_abbr} from the Project Master.

    **Where** the file lives is portfolio.find_project_file()'s job, not this
    module's -- it checks this machine's own data/project_master/ and then the
    team's shared "Smartsheet Portfolio" folder. This used to glob data/ itself,
    a second independent copy of that logic, and the two drifted the moment the
    shared folder was added (2026-08-03): a fresh install with no local copy
    could list the whole portfolio via get_portfolio_list and still fail here
    with "no Project Master found in data/". Found in live use, not in testing.

    Only the parsing below is this module's own, because it wants a cheap
    {name: state} map rather than portfolio.py's fuller records.
    """
    properties = {}

    chosen = None
    try:
        from portfolio import find_project_file
        found = find_project_file()
        if found is not None:
            chosen = str(found)
    except Exception as e:
        log.warning(f"[PROXIMITY] Could not locate the Project Master via portfolio.py: {e}")

    if chosen is None:
        # Legacy fallback: the original data/-only glob, kept so an unusual
        # local layout that worked before still does.
        pm_dir = data_dir / "project_master"
        search_dir = pm_dir if pm_dir.exists() else data_dir
        candidates = sorted(
            [f for f in glob.glob(str(search_dir / "*"))
             if Path(f).suffix.lower() in (".csv", ".xlsx", ".xls", ".pdf")
             and not Path(f).name.startswith(".")],
            key=lambda f: {".csv": 0, ".xlsx": 1, ".xls": 2, ".pdf": 3}.get(
                Path(f).suffix.lower(), 9)
        )
        pm_files = [f for f in candidates if any(
            kw in Path(f).name.lower()
            for kw in ["project", "master", "vaulter", "portfolio"]
        )] or candidates
        if not pm_files:
            log.warning("[PROXIMITY] No Project Master found locally or in the shared folder")
            return properties
        chosen = pm_files[0]

    ext = Path(chosen).suffix.lower()
    log.info(f"[PROXIMITY] Reading Project Master: {Path(chosen).name}")

    try:
        if ext == ".csv":
            with open(chosen, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    name = (row.get("Project Name") or "").strip()
                    state = _norm_state((row.get("State") or "").strip())
                    if name and name != "Template" and "@" not in name and state:
                        properties[name] = state

        elif ext in (".xlsx", ".xls"):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(chosen, read_only=True, data_only=True)
                ws = wb.active
                header = []
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i == 0:
                        header = [str(c or "").strip() for c in row]
                        continue
                    rd = dict(zip(header, [str(c or "").strip() for c in row]))
                    name = rd.get("Project Name", "").strip()
                    state = _norm_state(rd.get("State", "").strip())
                    if name and name != "Template" and "@" not in name and state:
                        properties[name] = state
                wb.close()
            except ImportError:
                log.warning("[PROXIMITY] openpyxl not installed — can't read Excel")

        elif ext == ".pdf":
            try:
                import pdfplumber
                with pdfplumber.open(chosen) as pdf:
                    for page in pdf.pages:
                        table = page.extract_table()
                        if not table:
                            continue
                        for row in table:
                            if not row or len(row) < 2:
                                continue
                            name = (row[0] or "").strip()
                            if not name or "@" in name or name in (
                                    "Project Name", "Template", "Project Sponsor"):
                                continue
                            state = ""
                            for ci in [3, 2, 1]:
                                if ci < len(row):
                                    s = _norm_state((row[ci] or "").strip())
                                    if s:
                                        state = s
                                        break
                            if name and state and name not in properties:
                                properties[name] = state
            except ImportError:
                log.warning("[PROXIMITY] pdfplumber not installed — can't read PDF")

    except Exception as e:
        log.warning(f"[PROXIMITY] Project Master read error: {e}")

    return properties


# ── Distance / direction helpers ───────────────────────────────────────────
def _dist_dir(origin_lat, origin_lon, dest_lat, dest_lon):
    """Return (distance_miles, cardinal_direction)."""
    try:
        from geopy.distance import geodesic
        dist = round(geodesic(
            (origin_lat, origin_lon), (dest_lat, dest_lon)).miles, 2)
    except Exception:
        R = 3958.8
        lat1, lat2 = math.radians(origin_lat), math.radians(dest_lat)
        dlat = lat2 - lat1
        dlon = math.radians(dest_lon - origin_lon)
        a = (math.sin(dlat/2)**2
             + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2)
        dist = round(R * 2 * math.asin(math.sqrt(a)), 2)

    lat1, lat2 = math.radians(origin_lat), math.radians(dest_lat)
    dlon = math.radians(dest_lon - origin_lon)
    x = math.sin(dlon) * math.cos(lat2)
    y = (math.cos(lat1) * math.sin(lat2)
         - math.sin(lat1) * math.cos(lat2) * math.cos(dlon))
    bearing = (math.degrees(math.atan2(x, y)) + 360) % 360
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dist, dirs[round(bearing / 22.5) % 16]


# ── Highway extraction ─────────────────────────────────────────────────────
HIGHWAY_PATTERNS = [
    (r"\bInterstate\s+(\d+[A-Z]?)\b",           "Interstate"),
    (r"\bI-(\d+[A-Z]?)\b",                       "Interstate"),
    (r"\bUS(?:\s+|-)Highway\s+(\d+[A-Z]?)\b",   "US Highway"),
    (r"\bU\.S\.\s+(\d+[A-Z]?)\b",               "US Highway"),
    (r"\bUS-(\d+[A-Z]?)\b",                      "US Highway"),
    (r"\bState\s+Highway\s+(\d+[A-Z]?)\b",       "State Highway"),
    (r"\bState\s+Route\s+(\d+[A-Z]?)\b",         "State Route"),
    (r"\bSH-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bTX-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bAZ-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bCA-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bCO-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bNM-(\d+[A-Z]?)\b",                      "State Highway"),
    (r"\bFarm\s+to\s+Market\s+Road\s+(\d+[A-Z]?)\b", "Farm to Market Road"),
    (r"\bFM\s+(\d+[A-Z]?)\b",                    "Farm to Market Road"),
    (r"\bFM-(\d+[A-Z]?)\b",                      "Farm to Market Road"),
    (r"\bCounty\s+Road\s+(\d+[A-Z]?)\b",         "County Road"),
    (r"\bCR\s+(\d+[A-Z]?)\b",                    "County Road"),
]


def _extract_highways(records: list, lat: float, lon: float,
                      color: str = "#717D7E") -> list:
    """
    Extract named highways and roads from the addresses of already-found
    businesses. Reliable because Google always includes road names in addresses.

    SUPERSEDED 2026-07-29 and no longer called -- see `_corridor_records`.
    It was written against Google Places addresses, which name the road; OSM
    `addr:street` almost never carries a highway NUMBER, which every pattern
    below requires. Measured across two real runs (201 result rows, Florence
    and Casa Grande AZ) it produced **zero** rows, while a direct Overpass
    query at the same point found Hunt Highway, Felix Road, the UP Phoenix
    Subdivision and the Copper Basin Railway. Left in place rather than
    deleted; deletion of this function and HIGHWAY_PATTERNS is proposed.
    """
    category = "Transportation & Infrastructure"
    icon = "🛣️"
    highway_mentions = {}

    for r in records:
        addr = r.get("address", "") + " " + r.get("notes", "")
        for pattern, road_type in HIGHWAY_PATTERNS:
            for num in re.findall(pattern, addr, re.IGNORECASE):
                if road_type == "Interstate":
                    label = f"I-{num}"
                elif road_type == "US Highway":
                    label = f"US-{num}"
                elif road_type in ("State Highway", "State Route"):
                    label = f"State Highway {num}"
                elif road_type == "Farm to Market Road":
                    label = f"FM {num}"
                elif road_type == "County Road":
                    label = f"CR {num}"
                else:
                    label = f"{road_type} {num}"

                if label not in highway_mentions:
                    highway_mentions[label] = {"road_type": road_type, "dists": []}
                highway_mentions[label]["dists"].append(r["distance_miles"])

    results = []
    for label, info in highway_mentions.items():
        min_dist = round(min(info["dists"]), 2)
        count = len(info["dists"])
        results.append({
            "name":           label,
            "category":       category,
            "icon":           icon,
            "color":          color,
            "address":        f"{count} business{'es' if count > 1 else ''} on this road within radius",
            "latitude":       lat,
            "longitude":      lon,
            "distance_miles": min_dist,
            "direction":      "N/A",
            "distance_label": f"~{min_dist} mi (nearest business on road)",
            "rating":         "",
            "source":         "Derived from Google Places addresses",
            "notes":          info["road_type"],
        })

    results.sort(key=lambda x: x["distance_miles"])
    return results


# ── Road & rail corridors ──────────────────────────────────────────────────
# Highway and rail access is a first-order factor for raw land, and the POI
# query cannot see it: OSM maps a road or a rail line as a *way* carrying no
# amenity/shop/landuse tag, so nothing in PROXIMITY_CATEGORIES ever matches
# one. This adds a second `out` statement to the SAME Overpass request (no
# extra HTTP call) asking for the ways themselves with their geometry, so the
# distance reported is to the nearest point ON the road, not to some midpoint.
CORRIDOR_CLASSES = {
    "motorway":  "Freeway",
    "trunk":     "Highway",
    "primary":   "Primary arterial",
    "secondary": "Secondary arterial",
    "rail":      "Freight / main line rail",
    "light_rail": "Light rail",
}

_CARDINAL_PREFIX = re.compile(r"^(North|South|East|West|N|S|E|W)\s+", re.IGNORECASE)


def _corridor_statement(lat: float, lon: float, radius_m: int) -> str:
    return (f'(way["highway"~"^(motorway|trunk|primary|secondary)$"]'
            f'(around:{radius_m},{lat},{lon});'
            f'way["railway"~"^(rail|light_rail)$"]'
            f'(around:{radius_m},{lat},{lon}););out geom 400;')


def _corridor_records(elements: list, lat: float, lon: float,
                      radius_miles: float, color: str) -> list:
    """Collapse the road/rail ways into one row per named route.

    A single highway is dozens of separate OSM ways; what a reader wants is
    "US-79, 1.4 mi W", once. Grouped by `ref` (the route number) where there
    is one, else by name."""
    routes = {}
    for el in elements:
        geom = el.get("geometry")
        if not geom:
            continue
        tags = el.get("tags", {})
        kind = tags.get("highway") or tags.get("railway") or ""
        cls = CORRIDOR_CLASSES.get(kind)
        if cls is None:
            continue

        ref, name = (tags.get("ref") or "").strip(), (tags.get("name") or "").strip()
        # One road, one row. OSM splits an arterial into directional segments
        # ("North Hunt Highway", "East Hunt Highway", "West Hunt Highway"), all
        # of which are Hunt Highway; without this the same road counts three
        # times and inflates the comparison. In a grid town the cardinal is a
        # quadrant marker, not a different street.
        label = ref or _CARDINAL_PREFIX.sub("", name).strip()
        if not label:
            continue

        best = None
        for pt in geom:
            d, direction = _dist_dir(lat, lon, pt["lat"], pt["lon"])
            if best is None or d < best[0]:
                best = (d, direction, pt["lat"], pt["lon"])
        if best is None or best[0] > radius_miles:
            continue

        prev = routes.get(label)
        if prev is None or best[0] < prev["distance_miles"]:
            routes[label] = {
                "name":           label,
                "category":       "Transportation & Infrastructure",
                "icon":           "🛣️",
                "color":          color,
                "address":        name if name != label else "",
                "latitude":       best[2],
                "longitude":      best[3],
                "distance_miles": best[0],
                "direction":      best[1],
                "distance_label": f"{best[1]} - {best[0]} mi (nearest point on route)",
                "rating":         "",
                "source":         "OpenStreetMap",
                "notes":          f"{cls} ({kind})",
            }

    return sorted(routes.values(), key=lambda x: x["distance_miles"])


# ── Main proximity search ──────────────────────────────────────────────────
def _classify(tags: dict, categories: list) -> dict | None:
    """Maps an OSM feature's tags to the first matching proximity category.

    First-match-wins, so category ORDER in config.PROXIMITY_CATEGORIES is
    meaningful -- e.g. a supermarket must be classified as "Grocery" before
    the broader "Retail & Big Box" gets a chance at it."""
    for cat in categories:
        for pair in cat.get("osm_tags", []):
            key, _, value = pair.partition("=")
            if tags.get(key) == value:
                return cat
    return None


def _usable_coord(lat, lon) -> bool:
    """A coordinate is mappable only if it is a real number in range and not
    the 0,0 null island a blank cell sometimes becomes downstream."""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if la != la or lo != lo:          # NaN
        return False
    if not (-90 <= la <= 90) or not (-180 <= lo <= 180):
        return False
    return not (la == 0 and lo == 0)


def coordinate_coverage(df) -> dict:
    """How many rows of a screened CoStar export can be mapped at all.

    WHY THIS EXISTS. Whether a file can be mapped is a property of the FILE,
    known the moment it is read, but until now it was only discoverable one
    refusal at a time: the 50-row Tucson export carries no coordinate column
    whatsoever, so every rank refuses individually and the reader has to infer
    the pattern. The refusal is correct and stays -- a name or a street address
    is not a location (geocoding portfolio names was measured wrong 5 times in
    8, twice in the wrong country). What was missing was saying so up front.

    Takes the screener's dataframe. Returns {total, mappable, message}, where
    `message` is empty when every row is mappable -- so a caller can print it
    unconditionally and stay silent when there is nothing wrong."""
    total = len(df)
    cols = list(df.columns)

    if "Latitude" not in cols or "Longitude" not in cols:
        mappable = 0
        why = "this export has no Latitude/Longitude column"
    else:
        mappable = sum(1 for la, lo in zip(df["Latitude"], df["Longitude"])
                       if _usable_coord(la, lo))
        why = "their coordinate cells are empty"

    if total and mappable == 0:
        message = (
            f"Mapping: none of the {total} listings in this file can be mapped — "
            f"{why}. run_proximity_for_listing will refuse on every rank, and it will "
            f"not fall back to the address or the property name: that guess was "
            f"measured wrong for 5 of 8 properties, twice in the wrong country, and it "
            f"fails silently. To map these, re-run the CoStar export with the Latitude "
            f"and Longitude columns included."
        )
    elif mappable < total:
        message = (f"Mapping: {mappable} of {total} listings carry coordinates; the "
                   f"other {total - mappable} cannot be mapped ({why}).")
    else:
        message = ""

    return {"total": total, "mappable": mappable, "message": message}


def run_proximity_search(property_name: str,
                         radius_miles: float,
                         vaulter_dir: Path,
                         lat: float = None,
                         lon: float = None) -> str:
    """
    Core proximity search logic. Called by the MCP tool in mcp_server.py.
    Reads all config from config.py — no hardcoding.
    Returns a formatted string summary.

    Two ways in:

      * by NAME — a property in the Project Master, whose coordinates are
        looked up in the hand-verified `property_coordinates.csv`. If it has
        none, this refuses rather than geocoding the name (see below).
      * by COORDINATE — `lat`/`lon` supplied directly, with `property_name`
        used only as a label. This is how a screened CoStar listing gets here:
        the export carries real coordinates for every row, so there is nothing
        to guess and the refusal below does not apply.

    Uses OpenStreetMap (Overpass + Nominatim) via analysis.screening
    .geo_providers -- free, keyless, no GOOGLE_PLACES_API_KEY required.

    Unlike the Google Places version, which needed one HTTP call per place
    type (~50 per run), this issues a SINGLE Overpass query for every
    category at once and classifies the results locally. That is both far
    faster and much kinder to a volunteer-run public endpoint that is
    already prone to "too busy" responses.
    """
    from pipeline import property_coordinates

    data_dir = vaulter_dir / "data"
    from config import PROXIMITY_OUTPUT_DIR
    prox_dir = PROXIMITY_OUTPUT_DIR
    prox_dir.mkdir(exist_ok=True)

    # ── Load config from config.py ───────────────────────────────
    try:
        categories, settings = _load_config()
    except (ImportError, Exception) as e:
        return f"Configuration error: {e}"

    # Use config default radius if caller passed 0 or didn't specify
    if not radius_miles:
        radius_miles = settings["default_radius_miles"]

    delay   = settings["places_request_delay_seconds"]
    timeout = settings["geocoding_timeout_seconds"]
    top_n   = settings["summary_results_per_category"]

    # ── Coordinate entry point ────────────────────────────────────
    # Supplied coordinates skip name resolution entirely. The refusal further
    # down guards against GEOCODING A NAME, which is unreliable; a coordinate
    # that came from the CoStar export is source data, not a guess.
    if lat is not None and lon is not None:
        matched_name = property_name or f"{lat:.5f},{lon:.5f}"
        location_note = ("Location from the listing's own coordinates — accurate to the "
                         "parcel but not its exact centre. Measured against county records "
                         "these sit 96-418 m from the true centroid, so distances below "
                         "are approximate")
        return _search_around(lat, lon, matched_name, location_note, radius_miles,
                              categories, settings, prox_dir,
                              subject_source="CoStar listing coordinates (not a Vaulter property)")

    # ── Load Project Master ───────────────────────────────────────
    properties = _load_project_master(data_dir)

    # ── Match property ────────────────────────────────────────────
    matched_name = matched_state = None
    if property_name in properties:
        matched_name = property_name
        matched_state = properties[property_name]
    else:
        matches = [(n, s) for n, s in properties.items()
                   if property_name.lower() in n.lower()]
        if len(matches) == 1:
            matched_name, matched_state = matches[0]
        elif len(matches) > 1:
            return (f"Multiple properties match '{property_name}':\n"
                    + "\n".join(f"  - {n}" for n, _ in matches)
                    + "\nPlease be more specific.")
        else:
            avail = "\n  ".join(sorted(properties)) if properties else (
                "(no Project Master found — publish the Smartsheet export to the "
                "team's 'Vaulter AI Shared/Smartsheet Portfolio' folder)")
            return (f"'{property_name}' not found in Project Master.\n\n"
                    f"Available properties:\n  {avail}")

    # ── Geocode ───────────────────────────────────────────────────
    clean = re.sub(r"\s*\(.*?\)", "", matched_name).strip()
    clean = re.sub(r"\s+\d+$", "", clean).strip().replace(" & ", " and ")
    geocode_query = f"{clean}, {matched_state}"

    # A stored coordinate always wins. It came from the property's own deed,
    # survey or title work, which is authoritative -- whereas geocoding a
    # property NAME is guesswork that fails outright for the ~40% of this
    # portfolio named after a street intersection (Nominatim cannot geocode
    # intersections at all). See pipeline/property_coordinates.py.
    stored = property_coordinates.lookup(data_dir, matched_name)
    if stored:
        lat, lon = stored["lat"], stored["lon"]
        location_note = (f"Location from stored coordinates "
                         f"({stored['precision']} precision"
                         f"{'; ' + stored['source'] if stored['source'] else ''})")
        if stored["precision"] == "city":
            location_note += (" — WARNING: only the town was identified for this "
                              "property, so distances below are indicative, not exact")
    else:
        # DELIBERATE REFUSAL -- do NOT add a geocode-the-name fallback here.
        #
        # Name geocoding was measured against this portfolio on 2026-07-27 and
        # is wrong more often than right: of 8 properties that returned a
        # result, 5 were wrong. Two California parcels named after a ranch and
        # a street resolved to Alberta and New Brunswick, CANADA. Three
        # adjacent Arizona parcels all resolved to a city in the wrong county,
        # ~30 miles from where they actually are.
        #
        # The failure is SILENT -- the tool would return a clean CSV of 100
        # businesses near the wrong town with nothing to flag it. Refusing is
        # loud and costs someone ten minutes; a wrong proximity report could
        # inform a real investment decision.
        return _no_coordinates_message(matched_name, data_dir)

    return _search_around(lat, lon, matched_name, location_note, radius_miles,
                          categories, settings, prox_dir,
                          subject_source=f"property_coordinates.csv "
                                         f"({stored['precision']} precision)")


def _no_coordinates_message(name: str, data_dir: Path) -> str:
    """The refusal, shared by the search and comparison entry points."""
    from pipeline import property_coordinates

    return (
        f"Refusing to run — no verified coordinates on file for '{name}'.\n\n"
        f"This tool no longer guesses a property's location from its name. That guess "
        f"is wrong more often than right (measured: 5 of 8 portfolio properties, two "
        f"of them in the wrong COUNTRY), and it fails silently — you would get a "
        f"perfectly normal-looking report for the wrong place.\n\n"
        f"To fix, add this property to:\n"
        f"  {property_coordinates.coords_path(data_dir)}\n\n"
        f"Take the location from the property's own recorded deed or title policy "
        f"(usually under '01. Legal\\Acquisition\\Title' or '...\\Closing Docs'). A "
        f"PLSS legal description — e.g. 'Section 18, Township 11 South, Range 11 East, "
        f"Gila and Salt River Meridian' — converts straight to coordinates via "
        f"analysis.screening.geo_providers.plss_section_centroid(). An APN or street "
        f"address works too. It only needs doing once per property."
    )


OSM_UNREACHABLE = (
    "OpenStreetMap (Overpass) could not be reached after retrying every "
    "mirror. This is a transient outage on a free public service, not a "
    "problem with the property — try again in a few minutes."
)


POI_LIMIT = 800


def _collect_records(lat: float, lon: float, radius_miles: float,
                     categories: list) -> tuple | None:
    """Every POI, road and rail route within the radius, nearest first.

    Returns (records, truncated) -- or None, never an empty list, when
    OpenStreetMap could not be reached, because "the mirror shrugged" and
    "there is nothing there" are different answers and only one of them is a
    finding about the land. `truncated` is True when the query hit its
    POI_LIMIT, which means the list is incomplete and the far end of the
    radius is under-represented; callers must say so rather than present a
    capped list as the whole picture.

    Shared by the export path (`_search_around`) and the comparison path
    (`compare_proximity`), so the two cannot drift apart."""
    from analysis.screening import geo_providers

    radius_m = int(radius_miles * 1609.34)

    # ── OpenStreetMap search: ONE query for every category ────────
    selectors = []
    for cat in categories:
        for pair in cat.get("osm_tags", []):
            key, _, value = pair.partition("=")
            selectors.append(f'nwr["{key}"="{value}"](around:{radius_m},{lat},{lon});')

    query = (f"[out:json][timeout:90];({''.join(selectors)});"
             f"out center tags {POI_LIMIT};"
             + _corridor_statement(lat, lon, radius_m))
    # empty_is_suspect: one mirror (overpass.osm.ch) answers HTTP 200 with zero
    # elements for places that plainly have features -- reproduced 2026-07-29,
    # this exact query returning 0 elements on one call and 61 on the next
    # three. Without this flag a proximity report can silently read "0 results
    # found" for a site with 60. See geo_providers._overpass.
    data = geo_providers._overpass(query, empty_is_suspect=True)
    if data is None:
        return None

    all_records = []
    elements = data.get("elements", [])
    # The corridor statement returns ways with geometry; everything else came
    # from the POI statement, which is the one that is capped.
    truncated = sum(1 for el in elements if "geometry" not in el) >= POI_LIMIT

    for el in elements:
        tags = el.get("tags", {})
        cat = _classify(tags, categories)
        if cat is None:
            continue

        # Nodes carry lat/lon directly; ways/relations return a `center`.
        dlat = el.get("lat", (el.get("center") or {}).get("lat"))
        dlon = el.get("lon", (el.get("center") or {}).get("lon"))
        if dlat is None or dlon is None:
            continue

        dist, direction = _dist_dir(lat, lon, dlat, dlon)
        if dist > radius_miles:
            continue

        # Unnamed features are still meaningful here (an unnamed substation
        # or landfill matters to a land buyer), so they are labelled by what
        # they are rather than discarded.
        matched_tag = next(
            (f"{k}={tags[k]}" for k in ("amenity", "shop", "landuse", "office",
                                        "leisure", "tourism", "power", "man_made",
                                        "railway", "aeroway", "building", "military")
             if k in tags),
            "",
        )
        name = tags.get("name") or f"(unnamed {matched_tag.split('=')[-1].replace('_', ' ')})"

        address = " ".join(filter(None, [
            tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:city"),
        ]))

        all_records.append({
            "name":           name,
            "category":       cat["label"],
            "icon":           cat.get("icon", "📍"),
            "color":          cat.get("color", "#888888"),
            "address":        address,
            "latitude":       dlat,
            "longitude":      dlon,
            "distance_miles": dist,
            "direction":      direction,
            "distance_label": f"{direction} - {dist} mi",
            "rating":         "",   # OSM has no ratings; column kept for CSV shape
            "source":         "OpenStreetMap",
            "notes":          matched_tag,
        })

    # ── Road & rail corridors ─────────────────────────────────────
    transport_color = "#717D7E"
    for cat in categories:
        if "transport" in cat["label"].lower() or "infrastructure" in cat["label"].lower():
            transport_color = cat.get("color", "#717D7E")
            break
    all_records.extend(_corridor_records(elements, lat, lon,
                                         radius_miles, transport_color))

    # ── De-duplicate ──────────────────────────────────────────────
    seen_keys, deduped = set(), []
    for r in all_records:
        key = (r["name"].lower().strip(),
               round(r["latitude"], 4), round(r["longitude"], 4))
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(r)
    deduped.sort(key=lambda x: x["distance_miles"])
    return deduped, truncated


def _search_around(lat: float, lon: float, matched_name: str, location_note: str,
                   radius_miles: float, categories: list, settings: dict,
                   prox_dir: Path, subject_source: str = "") -> str:
    """Everything after a location is known, shared by both entry points."""
    collected = _collect_records(lat, lon, radius_miles, categories)
    if collected is None:
        return OSM_UNREACHABLE
    deduped, truncated = collected

    # ── Export ────────────────────────────────────────────────────
    # One file per property/listing, overwritten on each run -- not
    # timestamped. A timestamped name meant re-running on the same site
    # while iterating just left the old result behind: confirmed
    # 2026-07-29 that the shared proximity_output folder had accumulated
    # 32 files, several pairs byte-identical re-runs of the same site
    # minutes apart, in the team-shared OneDrive folder with nothing ever
    # cleaning them up. The latest result is the only one anyone reads.
    #
    # CSV only -- the XLSX twin of every file was dropped 2026-07-29: it
    # carried the same rows as the CSV, so it was pure duplication in the
    # shared folder, not a different output.
    slug = matched_name.replace(" ", "_").replace("/", "-").replace("&", "and")
    csv_path = prox_dir / f"{slug}.csv"

    # ── Export CSV ────────────────────────────────────────────────
    fieldnames = ["name", "category", "address", "latitude", "longitude",
                  "distance_miles", "direction", "distance_label",
                  "rating", "source", "notes"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerow({
            "name":           f"[Subject] {matched_name}",
            "category":       "Subject Property",
            "latitude":       lat,
            "longitude":      lon,
            "distance_miles": 0,
            "direction":      "N/A",
            "distance_label": "Subject Property",
            "source":         subject_source or "Vaulter Project Master",
        })
        for r in deduped:
            w.writerow(r)

    log.info(f"[PROXIMITY] {matched_name} — {len(deduped)} results → {csv_path.name}")

    truncation_note = ""
    if truncated:
        truncation_note = (
            f"INCOMPLETE: OpenStreetMap returned the maximum {POI_LIMIT} features for "
            f"this radius, so this is a capped list, not everything there is. The far "
            f"end of the radius is under-represented — re-run with a smaller radius "
            f"for a complete picture of the near ground.\n"
        )

    return (
        f"Proximity search complete for {matched_name}.\n"
        f"Radius: {radius_miles} miles | {len(deduped)} unique results found.\n"
        f"{truncation_note}"
        f"{location_note}.\n"
        f"Data source: OpenStreetMap (no API key required).\n\n"
        f"File saved to {prox_dir}:\n"
        f"  CSV  -> {csv_path.name}"
    )


# ── Comparison ─────────────────────────────────────────────────────────────
# The two entry points above deliberately produce the same format so that a
# candidate and an owned property can be compared -- but nothing did the
# comparing, so answering "is this like the land we already own?" meant
# opening two spreadsheets and counting rows by eye. This does it.

def _site_summary(records: list) -> dict:
    """{category: {count, nearest, nearest_name}} for one site."""
    out = {}
    for r in records:
        e = out.setdefault(r["category"],
                           {"count": 0, "nearest": None, "nearest_name": ""})
        e["count"] += 1
        if e["nearest"] is None or r["distance_miles"] < e["nearest"]:
            e["nearest"] = r["distance_miles"]
            e["nearest_name"] = r["name"]
    return out


def compare_proximity(property_names: list,
                      radius_miles: float,
                      vaulter_dir: Path,
                      candidate_label: str = "",
                      candidate_lat: float = None,
                      candidate_lon: float = None) -> str:
    """Compare what is near a candidate listing and near owned properties.

    Every site is searched exactly as the single-site export searches it --
    same radius, same categories, same `_collect_records` -- so the counts are
    like for like. A candidate is given by coordinate (from the CoStar export,
    which is source data); owned properties are given by name and resolved
    through the hand-verified `property_coordinates.csv`, refusing outright if
    one has no verified coordinate. Nothing is geocoded from a name.

    Owned-vs-owned works too: pass two or more names and no candidate.

    Args:
        property_names:  portfolio property names to compare against
        radius_miles:    search radius; 0 uses the configured default
        vaulter_dir:     project root (for data/)
        candidate_label: label for the candidate, e.g. "LISTING 3 - 350 N 9th St"
        candidate_lat/lon: the candidate's coordinates from the export

    Returns a formatted string, and writes a CSV of the same table to
    the shared proximity folder alongside the single-site exports.
    """
    from pipeline import property_coordinates

    data_dir = vaulter_dir / "data"
    from config import PROXIMITY_OUTPUT_DIR
    prox_dir = PROXIMITY_OUTPUT_DIR
    prox_dir.mkdir(exist_ok=True)

    try:
        categories, settings = _load_config()
    except (ImportError, Exception) as e:
        return f"Configuration error: {e}"
    if not radius_miles:
        radius_miles = settings["default_radius_miles"]

    # ── Resolve every site to a coordinate ────────────────────────
    sites = []
    if candidate_lat is not None and candidate_lon is not None:
        sites.append({
            "label": candidate_label or f"{candidate_lat:.5f},{candidate_lon:.5f}",
            "lat": float(candidate_lat), "lon": float(candidate_lon),
            "note": "candidate — coordinates from the CoStar export itself",
        })
    for raw in (property_names or []):
        name = (raw or "").strip()
        if not name:
            continue
        stored = property_coordinates.lookup(data_dir, name)
        if stored is None:
            return _no_coordinates_message(name, data_dir)
        note = f"owned — {stored['precision']} precision"
        if stored["precision"] == "city":
            note += ", only the town was identified so its distances are indicative"
        sites.append({"label": name, "lat": stored["lat"], "lon": stored["lon"],
                      "note": note})

    if len(sites) < 2:
        return ("Nothing to compare — give at least two places: a candidate listing "
                "(by coordinate) plus one or more owned properties, or two owned "
                "properties.")
    if len(sites) > 4:
        return (f"{len(sites)} places is more than this will compare at once. Each site "
                f"is a separate query against a free, volunteer-run OpenStreetMap "
                f"endpoint; four is the limit. Pick the closest comparables.")

    # ── Search each one ───────────────────────────────────────────
    # Each site is its own Overpass query, so a busy endpoint can lose one of
    # them. A site that could not be searched is DROPPED and named, never shown
    # as a site with nothing nearby -- "the provider failed" and "there is
    # nothing there" are different answers.
    unreachable = []
    for s in sites:
        collected = _collect_records(s["lat"], s["lon"], radius_miles, categories)
        if collected is None:
            unreachable.append(s)
            continue
        recs, s["truncated"] = collected
        s["records"] = recs
        s["by_cat"] = _site_summary(recs)
        log.info(f"[PROXIMITY] compare: {s['label']} — {len(recs)} results")
    sites = [s for s in sites if "records" in s]

    if len(sites) < 2:
        return (f"{OSM_UNREACHABLE}\n\nToo few sites came back to compare "
                f"({', '.join(s['label'] for s in unreachable)} failed).")

    tags = ["A", "B", "C", "D"][:len(sites)]
    for tag, s in zip(tags, sites):
        s["tag"] = tag

    ordered = [c["label"] for c in categories]
    for s in sites:                       # any category not in config (none today)
        for cat in s["by_cat"]:
            if cat not in ordered:
                ordered.append(cat)
    present = [c for c in ordered if any(c in s["by_cat"] for s in sites)]
    absent = [c for c in ordered if c not in present]

    # ── Table ─────────────────────────────────────────────────────
    def cell(s, cat):
        e = s["by_cat"].get(cat)
        return f"{e['count']} / {e['nearest']:.1f}mi" if e else "—"

    width = 15
    lines = [f"Proximity comparison — {radius_miles} mile radius, OpenStreetMap", ""]
    for s in sites:
        lines.append(f"  {s['tag']}  {s['label']}  ({s['note']})")
    for s in unreachable:
        lines.append(f"  --  {s['label']}: LEFT OUT — OpenStreetMap could not be "
                     f"reached for it. That is a provider failure, not a finding "
                     f"about the site; re-run to include it.")
    lines += ["", "  " + "Category".ljust(34) + "".join(t.ljust(width) for t in tags),
              "  " + "-" * (34 + width * len(tags))]
    for cat in present:
        lines.append("  " + cat[:33].ljust(34)
                     + "".join(cell(s, cat).ljust(width) for s in sites))
    lines.append("  " + "TOTAL within radius".ljust(34)
                 + "".join(str(len(s["records"])).ljust(width) for s in sites))
    lines.append("")
    lines.append("  (count within radius / distance to the nearest one)")
    capped = [s for s in sites if s.get("truncated")]
    if capped:
        lines.append(f"  INCOMPLETE and NOT comparable like for like: "
                     f"{', '.join(s['tag'] for s in capped)} hit the {POI_LIMIT}-feature "
                     f"cap, so their counts are floors, not totals. Re-run at a smaller "
                     f"radius to compare fairly.")
    if absent:
        lines.append(f"  Nothing at any of them: {', '.join(absent)}")

    # ── Where they differ ─────────────────────────────────────────
    only, gaps = [], []
    for cat in present:
        have = [s for s in sites if cat in s["by_cat"]]
        lack = [s for s in sites if cat not in s["by_cat"]]
        if lack:
            got = ", ".join(f"{s['tag']} {s['by_cat'][cat]['count']} "
                            f"(nearest {s['by_cat'][cat]['nearest']:.1f} mi, "
                            f"{s['by_cat'][cat]['nearest_name']})" for s in have)
            only.append(f"  {cat}: {got} — none within {radius_miles} mi of "
                        f"{', '.join(s['tag'] for s in lack)}")
        else:
            near = min(have, key=lambda s: s["by_cat"][cat]["nearest"])
            far = max(have, key=lambda s: s["by_cat"][cat]["nearest"])
            dn, df_ = near["by_cat"][cat]["nearest"], far["by_cat"][cat]["nearest"]
            # Only worth saying when the difference is both large in absolute
            # terms and large in ratio -- 0.4 mi vs 0.9 mi is noise at this scale.
            if df_ - dn >= 1.0 and df_ >= 2 * dn:
                gaps.append(f"  {cat}: nearest is {dn:.1f} mi at {near['tag']} "
                            f"vs {df_:.1f} mi at {far['tag']}")

    lines.append("")
    lines.append("Where they differ")
    if only or gaps:
        if only:
            lines.append("  — present at some, absent at others —")
            lines += only
        if gaps:
            lines.append("  — same category, materially different distance —")
            lines += gaps
    else:
        lines.append("  Nothing separates them at this radius — every category that "
                     "appears, appears at all of them, at comparable distance.")

    # ── Export ────────────────────────────────────────────────────
    # One file per comparison, overwritten on each run -- see the same
    # note in _search_around() above for why this isn't timestamped.
    # CSV only -- see the same note in _search_around() above for why the
    # XLSX twin was dropped.
    slug = re.sub(r"[^A-Za-z0-9]+", "_", "_vs_".join(s["label"] for s in sites))[:80]
    csv_path = prox_dir / f"compare_{slug}.csv"

    header = ["category"]
    for s in sites:
        header += [f"{s['label']} count", f"{s['label']} nearest_mi",
                   f"{s['label']} nearest"]
    table = []
    for cat in present + absent:
        row = [cat]
        for s in sites:
            e = s["by_cat"].get(cat)
            row += [e["count"], e["nearest"], e["nearest_name"]] if e else [0, "", ""]
        table.append(row)
    table.append(["TOTAL within radius"]
                 + sum(([len(s["records"]), "", ""] for s in sites), []))

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(table)

    lines += ["", f"File saved to {prox_dir}:",
              f"  CSV  -> {csv_path.name}"]
    return "\n".join(lines)

