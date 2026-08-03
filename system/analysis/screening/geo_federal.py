"""
analysis/screening/geo_federal.py
---------------------------------
Ground-truth checks on a listing, from authoritative federal ArcGIS services.

Why this replaced the OpenStreetMap/Overpass path
-------------------------------------------------
Measured on four real Arizona parcels, same queries, same session:

    Overpass          success 1 of 3 attempts, 24-137s, and one public mirror
                      (overpass.osm.ch) returns HTTP 200 with an EMPTY body and
                      no error marker -- structurally valid, factually wrong.
                      Readings flapped run to run: one parcel returned
                      0, 21, 21, 0 nearby features; another reported
                      NO_ROAD_FOUND once and a named road twice.
    Census TIGERweb   4 of 4, 0.4s, identical answers every time.

Worse than the flakiness: OSM's *coverage* is thinnest exactly where this firm
buys. Overpass reported no road within 600m of a Florence parcel; TIGER returns
seven local roads there -- Mustang Way, Pinto Pony Dr, Twin Spurs Ln, Chaps Dr,
which is a platted horse-property subdivision. Volunteer mapping is sparse on
the rural urban edge; the Census road network is not.

Two rules this module exists to enforce
---------------------------------------
1. **Area, not point.** A centroid tells you about one spot, not a parcel. FEMA
   NFHL queried at the centroid of an 80-acre listing returned Zone X, "minimal
   hazard", not in an SFHA -- which read as CoStar's High Risk flag being wrong.
   The same parcel queried over its footprint returns an **AE zone (SFHA=T)**.
   CoStar was right. Every hazard check here covers the parcel's area.

2. **Unavailable is never a finding.** Each result carries an explicit status.
   "No SFHA found" and "the service did not answer" are different facts and are
   never collapsed. See the `status` values on every return.
"""

import json
import logging
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger("vaulter.geo_federal")

USER_AGENT = "VaulterAI/1.0 (land screening; contact via vaulterup.com)"

# Census TIGERweb -- the official US road network. Layer 8 is Local Roads;
# layer 2 is Primary only and misses everything residential, which is what an
# early version of this queried and got zero results from.
TIGER_ROADS_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services"
                   "/TIGERweb/Transportation/MapServer/8/query")
# Layer 4 is Incorporated Places. Layer 0 in this service is "Estates", a
# Puerto Rico geography that matches nothing in the continental US -- querying
# it returned zero features for every Arizona parcel tested, which rendered as
# a confident "UNINCORPORATED, annexation required" on all of them. Wrong layer,
# invented finding. Verify the layer index against the service's own metadata
# before trusting any ArcGIS answer.
TIGER_PLACES_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services"
                    "/TIGERweb/Places_CouSub_ConCity_SubMCD/MapServer/4/query")
FEMA_NFHL_URL = ("https://hazards.fema.gov/arcgis/rest/services/public"
                 "/NFHL/MapServer/28/query")
USGS_ELEVATION_URL = "https://api.opentopodata.org/v1/ned10m"

SQ_M_PER_ACRE = 4046.86


# ─── Transport ────────────────────────────────────────────────────────────────

def _arcgis(url: str, params: dict, attempts: int = 3, timeout: int = 30) -> dict | None:
    """
    GET an ArcGIS REST endpoint, returning parsed JSON or None.

    None means "we could not find out", never "there is nothing there" -- the
    callers depend on that distinction and surface it as UNAVAILABLE.

    Retries cover two failure shapes seen in practice: a TCP reset mid-handshake
    (FEMA does this intermittently) and ArcGIS's in-band `{"error": ...}` served
    with HTTP 200, which requests treats as success.
    """
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params={**params, "f": "json"},
                                headers={"User-Agent": USER_AGENT}, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if "error" not in data:
                    return data
                log.debug(f"ArcGIS in-band error from {url}: {data.get('error')}")
        except (requests.RequestException, ValueError) as e:
            log.debug(f"ArcGIS request failed ({type(e).__name__}) on {url}")
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


def parcel_envelope(lat: float, lng: float, acres: float) -> str:
    """
    A square footprint of `acres`, centred on the listing's coordinate, as an
    ArcGIS envelope.

    This is an APPROXIMATION of the parcel and is documented as such wherever
    it surfaces. Real parcels are rarely square and the listing coordinate is
    rarely the true centroid. It is still far better than a point: a point
    misses hazards on 99% of the land it claims to describe. Where a county
    parcel polygon is available (see parcel_registry.json), prefer that.
    """
    side_m = math.sqrt(max(acres, 0.1) * SQ_M_PER_ACRE)
    dlat = (side_m / 2) / 111_320
    dlng = (side_m / 2) / (111_320 * math.cos(math.radians(lat)))
    return f"{lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat}"


# ─── Flood, over the parcel's area ────────────────────────────────────────────

def flood_over_parcel(lat: float, lng: float, acres: float) -> dict:
    """
    FEMA flood zones intersecting the parcel footprint.

    Returns the set of zones present and whether ANY Special Flood Hazard Area
    touches the parcel. It deliberately does NOT report a percentage of acreage
    affected: computing intersection area needs real polygon geometry
    (shapely), which is not a dependency here, and inventing a number would be
    worse than admitting the limit. "An SFHA touches this parcel, get a survey"
    is the honest and actionable output.

    Per docs/COMPANY_PROFILE.md §5 floodplain is NOT a dealbreaker for this firm
    -- the deal record includes an acquisition with a meaningful share of its
    acreage in the 100-year floodplain. It matters because it reduces NET
    developable acreage, which is how the firm actually prices land.
    """
    data = _arcgis(FEMA_NFHL_URL, {
        "where": "1=1",
        "geometry": parcel_envelope(lat, lng, acres),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
        "returnGeometry": "false",
    })
    if data is None:
        return {"status": "UNAVAILABLE", "zones": None, "sfha_present": None,
                "note": "FEMA NFHL did not answer; flood risk is unknown, not absent"}

    features = data.get("features", [])
    if not features:
        return {"status": "NOT_MAPPED", "zones": [], "sfha_present": None,
                "note": "No mapped flood zone over this footprint. Common for "
                        "unmapped rural areas -- absence of data, not of risk"}

    zones, sfha = {}, False
    for f in features:
        a = f.get("attributes", {})
        zone = a.get("FLD_ZONE") or "?"
        subty = a.get("ZONE_SUBTY")
        label = f"{zone}" + (f" ({subty})" if subty else "")
        zones[label] = zones.get(label, 0) + 1
        if str(a.get("SFHA_TF", "")).upper().startswith("T"):
            sfha = True

    return {
        "status": "OK",
        "zones": sorted(zones),
        "sfha_present": sfha,
        "note": ("A Special Flood Hazard Area intersects this parcel footprint — "
                 "confirm NET developable acreage before pricing"
                 if sfha else
                 "No SFHA over the footprint; zones present are minimal-hazard"),
        "method": "envelope approximating the parcel from its stated acreage",
    }


# ─── Roads / access ───────────────────────────────────────────────────────────

def roads_near(lat: float, lng: float, radius_m: int = 600) -> dict:
    """Named roads within `radius_m`, from the Census TIGER road network."""
    data = _arcgis(TIGER_ROADS_URL, {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "distance": str(radius_m),
        "units": "esriSRUnit_Meter",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "NAME,MTFCC",
        "returnGeometry": "false",
    })
    if data is None:
        return {"status": "UNAVAILABLE", "count": None, "named": [],
                "note": "Census TIGER did not answer; access is unknown, not absent"}

    feats = data.get("features", [])
    named = sorted({f["attributes"].get("NAME") for f in feats
                    if f.get("attributes", {}).get("NAME")})
    if not feats:
        return {"status": "NO_ROAD_FOUND", "count": 0, "named": [],
                "note": f"No TIGER road within {radius_m}m — possible access constraint"}
    return {"status": "OK", "count": len(feats), "named": named[:8],
            "note": f"{len(feats)} road segment(s) within {radius_m}m"}


def place_context(lat: float, lng: float) -> dict:
    """
    The incorporated place (city/town) containing the point, if any.

    Directly useful for this firm: an unincorporated parcel needs annexation
    before city utilities, which is a different entitlement path and timeline
    from one already inside a municipality.
    """
    data = _arcgis(TIGER_PLACES_URL, {
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "NAME,BASENAME,STATE",
        "returnGeometry": "false",
    })
    if data is None:
        return {"status": "UNAVAILABLE", "place": None}
    feats = data.get("features", [])
    if not feats:
        return {"status": "UNINCORPORATED", "place": None,
                "note": "Not inside an incorporated place — annexation likely "
                        "required for municipal utilities"}
    return {"status": "OK", "place": feats[0]["attributes"].get("NAME"),
            "note": "Inside an incorporated place"}


def elevation_profile(lat: float, lng: float) -> dict:
    """Elevation at the centre and four offsets, as a crude relief measure."""
    d = 0.002
    pts = [(lat, lng), (lat + d, lng), (lat - d, lng), (lat, lng + d), (lat, lng - d)]
    locations = "|".join(f"{a},{b}" for a, b in pts)
    for attempt in range(3):
        try:
            r = requests.get(USGS_ELEVATION_URL, params={"locations": locations},
                             headers={"User-Agent": USER_AGENT}, timeout=25)
            if r.status_code == 200:
                results = r.json().get("results", [])
                elevs = [x.get("elevation") for x in results if x.get("elevation") is not None]
                if elevs:
                    return {"status": "OK", "elevations": elevs,
                            "max_diff_m": round(max(elevs) - min(elevs), 1)}
        except (requests.RequestException, ValueError):
            pass
        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))
    return {"status": "UNAVAILABLE", "elevations": None, "max_diff_m": None}


# ─── Caching ──────────────────────────────────────────────────────────────────
# Geo facts about a coordinate do not change between runs, and the shared folder
# means one person's lookups serve the whole team.
#
# Bump CACHE_VERSION whenever a provider's QUERY or interpretation changes, so
# old entries are discarded rather than served. This is not hypothetical: the
# place lookup originally queried the wrong TIGER layer and returned
# "UNINCORPORATED" for everything. Fixing the layer did not fix the answers,
# because the cache kept replaying the wrong ones -- and being cached, they
# looked more trustworthy, not less. Guarding only against UNAVAILABLE is not
# enough; a confidently wrong cached value is the worse failure.
CACHE_VERSION = 2

def _cache_path() -> Path:
    from config import SHARED_DIR, GEO_CACHE_DIR
    p = GEO_CACHE_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p / "federal_geo_cache.json"


def _load_cache() -> dict:
    """Cached entries, or an empty dict if the cache was written by an older
    provider version (its answers may be wrong, not merely stale)."""
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if blob.get("cache_version") != CACHE_VERSION:
        log.info(f"Geo cache was written by provider version "
                 f"{blob.get('cache_version')}, discarding (current {CACHE_VERSION}).")
        return {}
    return blob.get("entries", {})


def _save_cache(entries: dict) -> None:
    from core import safe_io
    try:
        safe_io.save_json_atomic(_cache_path(),
                                 {"cache_version": CACHE_VERSION, "entries": entries})
    except Exception as e:
        log.warning(f"Could not write geo cache: {e}")


def verify_site(lat: float, lng: float, acres: float, use_cache: bool = True) -> dict:
    """
    Every ground-truth check for one listing, cached by rounded coordinate.

    Only successful lookups are cached. A UNAVAILABLE result is never stored --
    caching a failure would turn one bad afternoon into a permanent wrong answer.
    """
    if lat is None or lng is None or lat != lat or lng != lng:
        return {"status": "NO_COORDINATES"}

    key = f"{round(float(lat), 5)},{round(float(lng), 5)},{round(float(acres or 0), 1)}"
    cache = _load_cache() if use_cache else {}
    if key in cache:
        hit = dict(cache[key])
        hit["from_cache"] = True
        return hit

    result = {
        "status": "OK",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "flood": flood_over_parcel(lat, lng, acres or 1),
        "roads": roads_near(lat, lng),
        "place": place_context(lat, lng),
        "elevation": elevation_profile(lat, lng),
        "from_cache": False,
    }

    if all(result[k].get("status") != "UNAVAILABLE"
           for k in ("flood", "roads", "place", "elevation")):
        cache[key] = {k: v for k, v in result.items() if k != "from_cache"}
        _save_cache(cache)
    else:
        log.info("Not caching this site — at least one provider was unavailable.")

    return result
