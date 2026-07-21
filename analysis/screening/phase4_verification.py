"""
analysis/screening/phase4_verification.py
---------------------------------------------
Vaulter CoStar Listing Screener — Phase 4 Final Verification

Selects the final finalists from the top-N ranked listings using a tiering
rule based on Phase 3's own RECOMMENDATION text (not Composite_Score
alone), then runs REAL-WORLD verification via Google Maps Platform APIs
(Elevation, Places, Roads, Distance Matrix, Solar, Street View, Static
Maps, Address Validation, Air Quality) on just those finalists. A second,
deeper Claude call combines Phase 3's analysis with this new ground-truth
data for a final verdict.

TIERING RULE for selecting the finalists from the top N:
  Tier 1 (strongest): recommendation clearly says "Pursue"
  Tier 2 (caveated):  "Conditional pursue" / "Cautious pursue"
  Tier 3 (weakest):   "Pass"
  Selection fills Tier 1 first, then Tier 2, then Tier 3 if needed.
  Composite_Score breaks ties WITHIN a tier. If ALL are Tier 3, a loud
  warning is printed -- that's a real signal the whole batch may not be
  worth pursuing further.

IMPORTANT: the "distance to core market" calculation geocodes each
listing's own CoStar 'Market Name' field (e.g. 'Phoenix, AZ', 'Dallas, TX')
dynamically via Google Geocoding -- no hardcoded state or city.

NOTE: this always uses the in-memory Phase 3 results passed to it -- the
original costar disk-caching logic (find_latest_deep_analysis /
cached_analysis_is_stale / load_cached_analyses) was dropped and replaced
with a different, content-hash-based cache (see _cache_key/run_verification's
cache_dir param) that reuses a finalist's verdict -- and skips its Google
Maps enrichment call too -- if its raw record + Phase 3 analysis are
unchanged from a prior run.
"""

import hashlib
import json
import logging
import time
import base64
from pathlib import Path

import requests
import pandas as pd
import anthropic

import safe_io
from . import phase3_deep_analysis

log = logging.getLogger("vaulter.screening")

FINAL_N_DEFAULT = 10

PLACES_SEARCH_RADIUS_METERS = 8047  # ~5 miles

# Fixed test coordinates used ONLY to probe which APIs are enabled --
# not tied to any real listing. (Downtown Phoenix, arbitrary valid US point.)
_PROBE_LAT, _PROBE_LNG = 33.4484, -112.0740

# In-memory cache so each unique market only gets geocoded once per run,
# even if multiple finalists share the same Market Name. get_market_reference_point
# also checks the shared on-disk cache (cache_dir) on every miss, and
# merges just the one new entry back in on write (never overwriting the
# whole file) -- so a market geocoded by ANY team member, at ANY time
# (not just once per process lifetime), is never looked up again by
# anyone, and no one's contribution to the shared file is ever clobbered.
_MARKET_GEOCODE_CACHE = {}


def _api_denied(status: str) -> bool:
    return status in ("REQUEST_DENIED", "PERMISSION_DENIED", "ERROR")


def probe_available_apis(api_key: str, include_low_value_apis: bool = False,
                          cache_dir: Path | None = None) -> dict:
    """Tests every candidate Google API once against a fixed location and
    records which ones are actually enabled for this key. Called once at
    the start of a run -- individual listing enrichment then only calls
    whatever this probe found available, instead of assuming a fixed set.

    include_low_value_apis=False (the default) skips probing -- and
    therefore skips ever calling -- Solar and Air Quality. Both are
    already noted in their own functions below as low/dubious value for
    raw vacant land: Solar isn't designed for vacant land at all (its
    "not found" result is actually the expected positive signal), and Air
    Quality is informational-only with minor relevance to a land
    investment decision. Skipping them by default saves those Google API
    calls on every finalist. Pass True to include them anyway."""
    log.info("Probing which Google APIs are enabled for this key...")
    results = {}

    try:
        r = google_elevation(_PROBE_LAT, _PROBE_LNG, api_key)
        results["elevation"] = not _api_denied(r["status"])
    except Exception:
        results["elevation"] = False

    try:
        r = google_places_nearby(_PROBE_LAT, _PROBE_LNG, api_key)
        results["places"] = not _api_denied(r["status"])
    except Exception:
        results["places"] = False

    try:
        r = google_roads_snap(_PROBE_LAT, _PROBE_LNG, api_key)
        results["roads"] = not _api_denied(r["status"])  # NO_ROAD_FOUND is a valid enabled-but-empty result
    except Exception:
        results["roads"] = False

    try:
        r = get_market_reference_point("Phoenix, AZ", api_key, cache_dir=cache_dir)
        results["geocoding"] = r["status"] == "OK"
    except Exception:
        results["geocoding"] = False

    if results.get("geocoding"):
        try:
            r = google_distance_to_reference(_PROBE_LAT, _PROBE_LNG, _PROBE_LAT + 0.1, _PROBE_LNG + 0.1, api_key)
            results["distance_matrix"] = not _api_denied(r["status"])
        except Exception:
            results["distance_matrix"] = False
    else:
        results["distance_matrix"] = False

    try:
        r = google_static_satellite_image(_PROBE_LAT, _PROBE_LNG, api_key)
        results["static_maps"] = r["status"] == "OK"
    except Exception:
        results["static_maps"] = False

    try:
        r = google_streetview_check(_PROBE_LAT, _PROBE_LNG, api_key)
        results["streetview"] = r["status"] in ("OK", "NO_COVERAGE")  # NO_COVERAGE means API works, just no imagery here
    except Exception:
        results["streetview"] = False

    if include_low_value_apis:
        try:
            r = google_solar_potential(_PROBE_LAT, _PROBE_LNG, api_key)
            results["solar"] = r["status"] in ("OK", "NO_BUILDING_FOUND")  # NO_BUILDING_FOUND means API works
        except Exception:
            results["solar"] = False
    else:
        results["solar"] = False

    try:
        r = google_address_validation("1600 Amphitheatre Parkway, Mountain View, CA", api_key)
        results["address_validation"] = r["status"] == "OK"
    except Exception:
        results["address_validation"] = False

    if include_low_value_apis:
        try:
            r = google_air_quality(_PROBE_LAT, _PROBE_LNG, api_key)
            results["air_quality"] = r["status"] == "OK"
        except Exception:
            results["air_quality"] = False
    else:
        results["air_quality"] = False

    for api_name, available in results.items():
        status = "available" if available else "not enabled / unavailable"
        log.info(f"  {api_name}: {status}")

    return results


def get_market_reference_point(market_name: str, api_key: str, cache_dir: Path | None = None) -> dict:
    """Geocodes the listing's own CoStar 'Market Name' (e.g. 'Phoenix, AZ')
    to get a dynamic core-market reference point -- works for any market
    the CoStar export covers, no hardcoded state/city.

    If cache_dir is given (the pipeline passes SCREENING_OUTPUT_DIR, shared
    across the team), a market genuinely only ever gets geocoded once,
    ever, by anyone -- checked in-memory first (fast, for repeat lookups
    within this run), then the shared on-disk cache (in case another team
    member already geocoded it), before actually calling Google."""
    global _MARKET_GEOCODE_CACHE

    if not market_name or pd.isna(market_name):
        return {"status": "NO_MARKET_NAME", "lat": None, "lng": None, "label": None}

    if market_name in _MARKET_GEOCODE_CACHE:
        return _MARKET_GEOCODE_CACHE[market_name]

    cache_path = (cache_dir / "market_geocode_cache.json") if cache_dir else None
    if cache_path:
        disk_cache = safe_io.load_json(cache_path)
        if market_name in disk_cache:
            result = disk_cache[market_name]
            _MARKET_GEOCODE_CACHE[market_name] = result
            return result

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    resp = requests.get(url, params={"address": market_name, "key": api_key}, timeout=15)
    data = resp.json()

    if data.get("status") != "OK" or not data.get("results"):
        result = {"status": data.get("status", "ERROR"), "lat": None, "lng": None, "label": market_name}
    else:
        loc = data["results"][0]["geometry"]["location"]
        result = {"status": "OK", "lat": loc["lat"], "lng": loc["lng"], "label": market_name}

    _MARKET_GEOCODE_CACHE[market_name] = result
    if cache_path:
        # Merge just this one new entry into whatever's on disk right now
        # -- never overwrite with our own (possibly stale/incomplete)
        # in-memory dict, which could clobber another team member's
        # concurrently-added markets.
        safe_io.locked_json_update(cache_path, lambda current, k=market_name, r=result: {**current, k: r})
    return result


_VERDICT_TIER = {"pursue": 1, "conditional": 2, "pass": 3}


def extract_verdict(recommendation_text: str) -> str | None:
    """Looks for the controlled 'VERDICT: Pursue/Conditional/Pass' line the
    Phase 3/4 prompts require. Returns 'pursue'/'conditional'/'pass', or
    None if the model didn't produce a compliant line."""
    for line in (recommendation_text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            token = line.split(":", 1)[1].strip().lower()
            for word in _VERDICT_TIER:
                if token.startswith(word):
                    return word
    return None


def classify_tier(recommendation_text: str) -> int:
    """Groups a Phase 3/4 recommendation into 3 tiers. Prefers the explicit
    VERDICT: line the prompt requires; only falls back to guessing from
    free text if the model didn't produce one (e.g. an older cached
    analysis, or non-compliant output)."""
    verdict = extract_verdict(recommendation_text)
    if verdict:
        return _VERDICT_TIER[verdict]

    log.warning("[classify_tier] no 'VERDICT:' line found -- falling back "
                "to free-text guessing. This means the model didn't follow the "
                "required format; the tier below may be unreliable.")
    text = (recommendation_text or "").lower().strip()
    first_bullet = text.split("\n")[0] if text else ""

    if first_bullet.startswith("pass") or first_bullet.startswith("- pass"):
        return 3
    if "conditional pursue" in text or "cautious pursue" in text:
        return 2
    if "pursue" in text:
        return 1
    return 2  # uncertain phrasing -> treat as middle tier, not top


def select_finalists(top_listings: pd.DataFrame, analyses: dict, final_n: int = FINAL_N_DEFAULT) -> list:
    """Returns list of (address, tier, composite_score) sorted by tier then
    score, top final_n."""
    scored = []
    for addr, parsed in analyses.items():
        tier = classify_tier(parsed["RECOMMENDATION"])
        composite = parsed["Composite_Score"]
        scored.append((addr, tier, composite))

    if all(t == 3 for _, t, _ in scored):
        log.warning(
            "ALL listings in this batch were recommended 'Pass' by Phase 3. "
            "This entire batch may not be worth pursuing further. Review "
            "before spending Phase 4 API calls on these candidates."
        )

    scored.sort(key=lambda x: (x[1], -x[2]))
    return scored[:final_n]


# ---------------------------------------------------------------------------
# Google Maps Platform calls
# ---------------------------------------------------------------------------

def google_elevation(lat: float, lng: float, api_key: str) -> dict:
    """Samples 5 points (center + 4 offsets ~500m away) to estimate terrain
    roughness. This is a PROXY for slope -- not true parcel-boundary
    grading data, since we don't have the actual parcel polygon."""
    offset = 0.0045  # ~500m at this latitude
    points = [
        (lat, lng),
        (lat + offset, lng), (lat - offset, lng),
        (lat, lng + offset), (lat, lng - offset),
    ]
    locations = "|".join(f"{p[0]},{p[1]}" for p in points)
    url = "https://maps.googleapis.com/maps/api/elevation/json"
    resp = requests.get(url, params={"locations": locations, "key": api_key}, timeout=15)
    data = resp.json()

    if data.get("status") != "OK":
        return {"status": data.get("status", "ERROR"), "elevations": [], "max_diff_m": None}

    elevations = [r["elevation"] for r in data["results"]]
    max_diff = max(elevations) - min(elevations)
    return {"status": "OK", "elevations": elevations, "max_diff_m": round(max_diff, 1)}


def google_places_nearby(lat: float, lng: float, api_key: str) -> dict:
    """General nearby-places search -- proxy for surrounding development
    density and what's actually around the site."""
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    params = {
        "location": f"{lat},{lng}",
        "radius": PLACES_SEARCH_RADIUS_METERS,
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    if data.get("status") not in ("OK", "ZERO_RESULTS"):
        return {"status": data.get("status", "ERROR"), "count": None, "top_places": []}

    results = data.get("results", [])
    top_places = [
        {"name": r.get("name"), "types": r.get("types", [])[:2]}
        for r in results[:8]
    ]
    return {"status": "OK", "count": len(results), "top_places": top_places}


def google_roads_snap(lat: float, lng: float, api_key: str) -> dict:
    """Snaps the coordinate to the nearest real road -- confirms genuine
    road access rather than a landlocked parcel."""
    url = "https://roads.googleapis.com/v1/nearestRoads"
    params = {"points": f"{lat},{lng}", "key": api_key}
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    if resp.status_code != 200 or "error" in data:
        # A genuine API error (not enabled for this key, invalid key,
        # quota exceeded, etc.) -- NOT the same thing as "no road found,"
        # which is a legitimate result (status_code 200, empty
        # snappedPoints) for a real rural/landlocked parcel. Conflating
        # the two previously made an unconfigured Roads API look like
        # "possible landlocked parcel" in every Phase 4 verdict.
        error_status = data.get("error", {}).get("status", "ERROR")
        return {"status": error_status, "place_id": None}

    snapped = data.get("snappedPoints", [])
    if not snapped:
        return {"status": "NO_ROAD_FOUND", "place_id": None}
    return {"status": "OK", "place_id": snapped[0].get("placeId")}


def google_distance_to_reference(lat: float, lng: float, ref_lat: float, ref_lng: float, api_key: str) -> dict:
    """Drive time/distance from the listing to a dynamic reference point
    (the geocoded center of the listing's own CoStar Market Name)."""
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{lat},{lng}",
        "destinations": f"{ref_lat},{ref_lng}",
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=15)
    data = resp.json()

    try:
        element = data["rows"][0]["elements"][0]
        if element["status"] != "OK":
            return {"status": element["status"], "distance_mi": None, "duration_min": None}
        return {
            "status": "OK",
            "distance_mi": round(element["distance"]["value"] / 1609.34, 1),
            "duration_min": round(element["duration"]["value"] / 60, 0),
        }
    except (KeyError, IndexError):
        return {"status": "ERROR", "distance_mi": None, "duration_min": None}


def google_static_satellite_image(lat: float, lng: float, api_key: str, zoom: int = 18, size: str = "640x640") -> dict:
    """Fetches a satellite image centered on the listing's coordinates,
    for visual inspection of whether the land is genuinely vacant."""
    url = "https://maps.googleapis.com/maps/api/staticmap"
    params = {
        "center": f"{lat},{lng}",
        "zoom": zoom,
        "size": size,
        "maptype": "satellite",
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200 or resp.headers.get("content-type", "").startswith("text"):
        return {"status": "ERROR", "image_bytes": None}
    return {"status": "OK", "image_bytes": resp.content}


def google_address_validation(address: str, api_key: str) -> dict:
    """Validates/standardizes an address -- catches malformed addresses
    or confirms geocoding accuracy before trusting a listing's location."""
    url = f"https://addressvalidation.googleapis.com/v1:validateAddress?key={api_key}"
    body = {"address": {"addressLines": [address]}}
    resp = requests.post(url, json=body, timeout=15)

    if resp.status_code != 200:
        return {"status": "ERROR", "verdict": None}

    data = resp.json()
    try:
        verdict = data["result"]["verdict"]
        return {
            "status": "OK",
            "verdict": {
                "address_complete": verdict.get("addressComplete", False),
                "has_unconfirmed_components": verdict.get("hasUnconfirmedComponents", False),
            },
        }
    except KeyError:
        return {"status": "ERROR", "verdict": None}


def google_air_quality(lat: float, lng: float, api_key: str) -> dict:
    """Current air quality index near the site -- minor relevance for
    land value-add, but cheap to include if enabled."""
    url = f"https://airquality.googleapis.com/v1/currentConditions:lookup?key={api_key}"
    body = {"location": {"latitude": lat, "longitude": lng}}
    resp = requests.post(url, json=body, timeout=15)

    if resp.status_code != 200:
        return {"status": "ERROR", "aqi": None, "category": None}

    data = resp.json()
    try:
        index = data["indexes"][0]
        return {"status": "OK", "aqi": index.get("aqi"), "category": index.get("category")}
    except (KeyError, IndexError):
        return {"status": "ERROR", "aqi": None, "category": None}


def google_streetview_check(lat: float, lng: float, api_key: str, size: str = "640x480") -> dict:
    """Ground-level Street View image, if coverage exists at this location.
    Rural/undeveloped land often has NO Street View coverage -- that's a
    normal, expected outcome here, not an error."""
    metadata_url = "https://maps.googleapis.com/maps/api/streetview/metadata"
    meta_resp = requests.get(metadata_url, params={"location": f"{lat},{lng}", "key": api_key}, timeout=15)
    meta = meta_resp.json()

    if meta.get("status") != "OK":
        return {"status": "NO_COVERAGE", "image_bytes": None}

    image_url = "https://maps.googleapis.com/maps/api/streetview"
    img_resp = requests.get(
        image_url,
        params={"size": size, "location": f"{lat},{lng}", "key": api_key},
        timeout=15,
    )
    if img_resp.status_code != 200 or img_resp.headers.get("content-type", "").startswith("text"):
        return {"status": "ERROR", "image_bytes": None}
    return {"status": "OK", "image_bytes": img_resp.content}


def google_solar_potential(lat: float, lng: float, api_key: str) -> dict:
    """Google's Solar API analyzes EXISTING ROOFTOPS for solar potential --
    it is not designed for raw vacant land. For genuinely vacant parcels,
    'not found' is the EXPECTED, normal outcome, not a failure -- it's
    actually a mild positive signal (no structure detected)."""
    url = "https://solar.googleapis.com/v1/buildingInsights:findClosest"
    params = {"location.latitude": lat, "location.longitude": lng, "key": api_key}
    resp = requests.get(url, params=params, timeout=15)

    if resp.status_code == 404:
        return {"status": "NO_BUILDING_FOUND", "data": None}
    if resp.status_code != 200:
        return {"status": "ERROR", "data": None}

    data = resp.json()
    try:
        panel_count = data["solarPotential"]["maxArrayPanelsCount"]
        yearly_kwh = data["solarPotential"]["maxArrayAnnualEnergyDcKwh"]
        return {"status": "OK", "data": {"panel_count": panel_count, "yearly_kwh": round(yearly_kwh)}}
    except KeyError:
        return {"status": "NO_BUILDING_FOUND", "data": None}


def enrich_listing(row: pd.Series, api_key: str, available: dict, cache_dir: Path | None = None) -> dict:
    lat, lng = row.get("Latitude"), row.get("Longitude")
    if pd.isna(lat) or pd.isna(lng):
        return {"error": "No coordinates available for this listing"}

    market_name = row.get("Market Name")
    result = {}

    if available.get("geocoding"):
        log.info(f"    Geocoding market ({market_name})...")
        result["reference"] = get_market_reference_point(market_name, api_key, cache_dir=cache_dir)
        time.sleep(0.2)
    else:
        result["reference"] = {"status": "API_NOT_AVAILABLE", "lat": None, "lng": None, "label": None}

    if available.get("elevation"):
        log.info(f"    Elevation...")
        result["elevation"] = google_elevation(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["elevation"] = {"status": "API_NOT_AVAILABLE", "max_diff_m": None}

    if available.get("places"):
        log.info(f"    Places...")
        result["places"] = google_places_nearby(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["places"] = {"status": "API_NOT_AVAILABLE", "count": None, "top_places": []}

    if available.get("roads"):
        log.info(f"    Roads...")
        result["roads"] = google_roads_snap(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["roads"] = {"status": "API_NOT_AVAILABLE"}

    if available.get("static_maps"):
        log.info(f"    Satellite image...")
        result["satellite"] = google_static_satellite_image(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["satellite"] = {"status": "API_NOT_AVAILABLE", "image_bytes": None}

    if available.get("streetview"):
        log.info(f"    Street View...")
        result["streetview"] = google_streetview_check(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["streetview"] = {"status": "API_NOT_AVAILABLE", "image_bytes": None}

    if available.get("solar"):
        log.info(f"    Solar potential...")
        result["solar"] = google_solar_potential(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["solar"] = {"status": "API_NOT_AVAILABLE", "data": None}

    if available.get("distance_matrix") and result["reference"]["status"] == "OK":
        ref = result["reference"]
        log.info(f"    Distance to {ref['label']}...")
        result["distance_to_reference"] = google_distance_to_reference(lat, lng, ref["lat"], ref["lng"], api_key)
    else:
        result["distance_to_reference"] = {"status": "API_NOT_AVAILABLE", "distance_mi": None, "duration_min": None}
    result["reference_label"] = result["reference"].get("label") or "core market"

    if available.get("air_quality"):
        log.info(f"    Air quality...")
        result["air_quality"] = google_air_quality(lat, lng, api_key)
        time.sleep(0.2)
    else:
        result["air_quality"] = {"status": "API_NOT_AVAILABLE", "aqi": None, "category": None}

    if available.get("address_validation"):
        addr = row.get("Property Address", "")
        log.info(f"    Address validation...")
        result["address_validation"] = google_address_validation(addr, api_key)
        time.sleep(0.2)
    else:
        result["address_validation"] = {"status": "API_NOT_AVAILABLE", "verdict": None}

    return result


def format_enrichment_for_prompt(enrichment: dict) -> str:
    if "error" in enrichment:
        return f"GOOGLE API DATA: unavailable ({enrichment['error']})"

    lines = ["REAL-WORLD VERIFICATION DATA (Google Maps Platform -- only "
             "APIs confirmed enabled for this key were used):"]

    elev = enrichment["elevation"]
    if elev["status"] == "OK":
        lines.append(f"- Terrain: elevation varies by {elev['max_diff_m']}m across sampled points "
                      f"near the site (proxy for slope -- larger values suggest rougher terrain)")
    elif elev["status"] != "API_NOT_AVAILABLE":
        lines.append(f"- Terrain: elevation data unavailable ({elev['status']})")

    places = enrichment["places"]
    if places["status"] == "OK":
        place_names = ", ".join(p["name"] for p in places["top_places"][:5]) or "none found"
        lines.append(f"- Surrounding area: {places['count']} points of interest found within "
                      f"~5 miles. Nearest examples: {place_names}")
    elif places["status"] != "API_NOT_AVAILABLE":
        lines.append(f"- Surrounding area: Places data unavailable ({places['status']})")

    roads = enrichment["roads"]
    if roads["status"] == "OK":
        lines.append("- Road access: confirmed -- coordinate snaps to a real, mapped road")
    elif roads["status"] != "API_NOT_AVAILABLE":
        lines.append(f"- Road access: COULD NOT CONFIRM real road access ({roads['status']}) "
                      f"-- possible landlocked parcel, verify manually")

    dist = enrichment["distance_to_reference"]
    ref_label = enrichment.get("reference_label", "core market")
    if dist["status"] == "OK":
        lines.append(f"- Distance to {ref_label} (this listing's own CoStar market center): "
                      f"{dist['distance_mi']} miles, ~{int(dist['duration_min'])} min drive")
    elif dist["status"] != "API_NOT_AVAILABLE":
        lines.append(f"- Distance to {ref_label}: unavailable ({dist['status']})")

    solar = enrichment.get("solar", {"status": "API_NOT_AVAILABLE", "data": None})
    if solar["status"] == "OK":
        lines.append(f"- Solar potential: existing structure detected with rooftop solar "
                      f"capacity for ~{solar['data']['panel_count']} panels "
                      f"(~{solar['data']['yearly_kwh']:,} kWh/year) -- NOTE: this indicates "
                      f"an existing building was found, which may contradict vacant-land status")
    elif solar["status"] == "NO_BUILDING_FOUND":
        lines.append("- Solar potential: no existing rooftop/structure detected by Google's "
                      "Solar API -- consistent with genuinely vacant land (this is expected "
                      "and not a data failure)")
    # if API_NOT_AVAILABLE, omit entirely -- not worth mentioning to Claude

    addr_val = enrichment.get("address_validation", {"status": "API_NOT_AVAILABLE", "verdict": None})
    if addr_val["status"] == "OK" and addr_val["verdict"]:
        v = addr_val["verdict"]
        if v["has_unconfirmed_components"]:
            lines.append("- Address validation: some address components could NOT be confirmed -- "
                          "verify the listing address is accurate before proceeding")
        else:
            lines.append("- Address validation: address confirmed accurate")

    aq = enrichment.get("air_quality", {"status": "API_NOT_AVAILABLE", "aqi": None, "category": None})
    if aq["status"] == "OK":
        lines.append(f"- Air quality: AQI {aq['aqi']} ({aq['category']}) -- minor relevance for "
                      f"raw land, informational only")

    return "\n".join(lines)


def build_final_prompt(row: pd.Series, phase3_analysis: dict, enrichment_text: str, has_satellite: bool, has_streetview: bool) -> str:
    phase3_summary = "\n\n".join(
        f"{k}:\n{v}" for k, v in phase3_analysis.items() if k != "Composite_Score"
    )

    image_parts = []
    if has_satellite:
        image_parts.append("a satellite (aerial) image")
    if has_streetview:
        image_parts.append("a Street View (ground-level) image")

    if image_parts:
        pronoun = "them" if len(image_parts) > 1 else "it"
        image_note = f"Attached: {' and '.join(image_parts)} of this exact site. Actually look at {pronoun} --"
        image_instruction = (
            "describe what you see and state plainly whether the land appears\n"
            "genuinely vacant/raw, or whether you can see existing structures,\n"
            "pavement, buildings, or other development on it that contradicts a\n"
            '"vacant land" assumption. If both images are attached, note whether\n'
            "they agree with each other."
        )
        if not has_streetview:
            image_instruction += ("\nNote: no Street View coverage exists for this rural "
                                   "location -- this is normal and not itself a concern.")
    else:
        image_note = "NOTE: no satellite or Street View imagery could be retrieved for this site --"
        image_instruction = ("state in VISUAL_INSPECTION that no imagery was available and this\n"
                              "check could not be performed -- do not guess or assume vacancy.")

    return f"""You are doing FINAL due diligence verification for Vaulter
before this listing goes to leadership for a pursue decision. You
already produced an initial analysis (below). Now you have real-world
ground-truth data from Google Maps Platform that was not available
before. Update or confirm your verdict in light of this new information.

PROPERTY: {row.get('Property Address', 'Unknown')}

YOUR PRIOR ANALYSIS:
{phase3_summary}

{enrichment_text}

For RISK_ASSESSMENT specifically: your job is to hunt for anything that
could realistically KILL this deal -- not to restate minor concerns
already covered elsewhere. Think legal/regulatory blockers, fatal access
or utility problems, title defects, or anything the ground-truth data
just revealed that changes the picture. If you genuinely find nothing
at that level, say "No deal-killing risks identified" rather than
manufacturing a weak one.

{image_note}
{image_instruction}

Write your FINAL analysis in EXACTLY this format, with these headers
verbatim, each with 2-4 concise bullet points ("- "):

VISUAL_INSPECTION:
- <what you actually see in the satellite image -- vacant/raw land,
  or visible structures/pavement/development. Be specific and honest;
  if the image is unclear or inconclusive, say so>

GROUND_TRUTH_FINDINGS:
- <bullet>
- <bullet>

RISK_ASSESSMENT:
- <something that could realistically KILL this deal -- e.g. a hard
  legal/regulatory blocker, a fatal access or utility problem, a title
  issue -- not a minor concern. If nothing rises to that level, say so
  explicitly rather than inventing a weak risk>
- <another potential deal-killer, if one exists>

FINAL_RECOMMENDATION:
VERDICT: <Pursue, Conditional, or Pass -- exactly one of these three
  words on its own line, nothing else. This is parsed by code, so it
  must match one of "VERDICT: Pursue", "VERDICT: Conditional", or
  "VERDICT: Pass" verbatim>
- <bullet explaining the verdict, updated in light of the ground-truth
  data above if it changes anything>

REMAINING_DILIGENCE_ITEMS:
- <specific action to take before an LOI>
- <another action>
"""


def parse_final_response(text: str) -> dict:
    sections = {
        "VISUAL_INSPECTION": [],
        "GROUND_TRUTH_FINDINGS": [],
        "RISK_ASSESSMENT": [],
        "FINAL_RECOMMENDATION": [],
        "REMAINING_DILIGENCE_ITEMS": [],
    }
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        header_line = phase3_deep_analysis.normalize_header_line(stripped)
        matched = False
        for key in sections:
            if header_line.startswith(f"{key}:"):
                current = key
                remainder = header_line[len(key) + 1:].strip()
                if remainder:
                    sections[key].append(remainder)
                matched = True
                break
        if not matched and current and stripped:
            sections[current].append(stripped)
    return {k: "\n".join(v) for k, v in sections.items()}


_FINAL_PROMPT_VERSION = "v1"  # bump if build_final_prompt's format changes meaningfully


def _final_cache_key(row: pd.Series, phase3_analysis: dict) -> str:
    # Uses _cacheable_record_text (not build_full_record_text) so the key
    # excludes Phase 1/2's batch-dependent Score_*/Composite_Score/
    # Screening_* columns -- otherwise the same physical listing hashes
    # differently between runs purely because other rows in the file
    # changed, defeating this cache for a listing whose own data (and
    # whose Phase 3 analysis) is genuinely unchanged.
    full_record = phase3_deep_analysis._cacheable_record_text(row)
    phase3_repr = json.dumps(
        {k: v for k, v in phase3_analysis.items() if k != "Composite_Score"},
        sort_keys=True,
    )
    return hashlib.sha256((_FINAL_PROMPT_VERSION + full_record + phase3_repr).encode()).hexdigest()


def run_verification(
    ranked_df: pd.DataFrame,
    deep_analyses: dict,
    anthropic_api_key: str,
    google_api_key: str | None,
    top_n: int = phase3_deep_analysis.TOP_N_DEFAULT,
    final_n: int = FINAL_N_DEFAULT,
    cache_dir: Path | None = None,
    include_low_value_apis: bool = False,
) -> dict:
    """
    Gets the top listings via phase3_deep_analysis.get_top_listings, calls
    select_finalists using deep_analyses for tiering, and:

    - if google_api_key is falsy: returns {"skipped": True, "reason": ...,
      "finalists": [...addresses in order...]} -- still computes the
      finalist order even without enrichment, so the workbook can show
      which rows would be finalists.
    - otherwise: probes available Google APIs, enriches each finalist,
      builds the final multimodal Claude prompt per finalist exactly as
      the original did, and returns {"skipped": False, "finalists": [...],
      "analyses": {address: {section_name: text}}}.

    If cache_dir is given (the pipeline passes SCREENING_OUTPUT_DIR, shared
    across the team), a finalist's verdict is cached by a hash of its raw
    record + Phase 3 analysis -- if unchanged from a prior run, BOTH the
    Google Maps enrichment call AND the Claude call are skipped for that
    finalist (probe_available_apis itself is also deferred until the first
    genuine cache miss, so an all-cache-hit run makes zero Google calls).
    Note: this assumes the real-world ground-truth data (imagery, nearby
    places, etc.) hasn't meaningfully changed since the cached run -- a
    reasonable tradeoff for cost savings, not appropriate if you need to
    force a fresh ground-truth check on a listing you already have cached.

    include_low_value_apis=False (the default) skips Solar and Air Quality
    -- see probe_available_apis for why those two specifically are safe to
    skip by default.
    """
    top_listings = phase3_deep_analysis.get_top_listings(ranked_df, top_n)
    finalist_tuples = select_finalists(top_listings, deep_analyses, final_n=final_n)
    finalist_addresses = [addr for addr, _tier, _score in finalist_tuples]

    if not google_api_key:
        return {
            "skipped": True,
            "reason": "no Google Maps API key configured",
            "finalists": finalist_addresses,
        }

    cache_path = (cache_dir / "phase4_verdict_cache.json") if cache_dir else None
    cache = safe_io.load_json(cache_path) if cache_path else {}
    cache_hits = 0

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    available_apis = None  # probed lazily -- only once a cache miss actually needs it

    final_results = {}
    for addr, tier, score in finalist_tuples:
        row = top_listings[top_listings["_Screening_Key"] == addr].iloc[0]
        key = _final_cache_key(row, deep_analyses[addr])

        if key in cache:
            parsed_final = dict(cache[key])
            cache_hits += 1
        else:
            try:
                if available_apis is None:
                    available_apis = probe_available_apis(
                        google_api_key, include_low_value_apis=include_low_value_apis, cache_dir=cache_dir,
                    )

                enrichment = enrich_listing(row, google_api_key, available_apis, cache_dir=cache_dir)
                enrichment_text = format_enrichment_for_prompt(enrichment)

                satellite = enrichment.get("satellite", {"status": "ERROR", "image_bytes": None})
                streetview = enrichment.get("streetview", {"status": "ERROR", "image_bytes": None})
                has_satellite = satellite["status"] == "OK" and satellite["image_bytes"] is not None
                has_streetview = streetview["status"] == "OK" and streetview["image_bytes"] is not None

                prompt = build_final_prompt(row, deep_analyses[addr], enrichment_text, has_satellite, has_streetview)

                content = []
                if has_satellite:
                    image_b64 = base64.standard_b64encode(satellite["image_bytes"]).decode("utf-8")
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": image_b64},
                    })
                if has_streetview:
                    sv_b64 = base64.standard_b64encode(streetview["image_bytes"]).decode("utf-8")
                    content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": sv_b64},
                    })
                content.append({"type": "text", "text": prompt})

                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1400,
                    messages=[{"role": "user", "content": content}],
                )
                if not response.content:
                    raise ValueError("Claude returned an empty response (no content blocks)")
                parsed_final = parse_final_response(response.content[0].text)
                if cache_path:
                    safe_io.locked_json_update(cache_path, lambda current, k=key, p=parsed_final: {**current, k: dict(p)})
            except Exception as e:
                # A single finalist's Google Maps/Claude call failing (rate
                # limit, empty response, transient network error) must not
                # abort the whole Phase 4 batch and lose every OTHER
                # finalist's verdict. Record a clearly-flagged placeholder
                # so this one is still visible for manual follow-up instead
                # of crashing the run or silently disappearing.
                log.error(f"  Phase 4 verification failed for '{addr}': {e}")
                parsed_final = {
                    "VISUAL_INSPECTION": "", "GROUND_TRUTH_FINDINGS": "", "RISK_ASSESSMENT": "",
                    "FINAL_RECOMMENDATION": f"VERIFICATION FAILED -- needs manual review ({e})",
                    "REMAINING_DILIGENCE_ITEMS": "",
                }

        parsed_final["Composite_Score"] = deep_analyses[addr]["Composite_Score"]
        parsed_final["Phase3_Recommendation"] = deep_analyses[addr]["RECOMMENDATION"]
        final_results[addr] = parsed_final

    if cache_path and cache_hits:
        log.info(f"Phase 4: reused {cache_hits}/{len(finalist_tuples)} cached finalist "
                  f"verdicts -- no new Claude/Google Maps calls for those.")

    return {
        "skipped": False,
        "finalists": finalist_addresses,
        "analyses": final_results,
    }
