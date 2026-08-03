"""
Keyless geospatial providers for the proximity export and aerial imagery.

Most of this module served the old `phase4_verification.py`. When that was
deleted, its flood, road, nearby-places and routing calls went with it —
`geo_federal.py` does that work now, against authoritative federal sources, and
checks flood over the parcel's AREA rather than a single centre point.

What remains has live callers and no federal equivalent:

  _overpass              OpenStreetMap Overpass   -> proximity_tool's POI category search
  geocode                Nominatim                -> proximity_tool
  plss_section_centroid  BLM PLSS                 -> resolving deeds by section/township/range
  parcel_by_apn          California parcel layer  -> resolving deeds by APN
  elevation_profile      Open Topo Data (USGS)    -> kept alongside geocode
  satellite_image        Planetary Computer NAIP  -> report.py's aerial views

`plss_section_centroid` and `parcel_by_apn` exist for populating
`data/project_master/property_coordinates.csv`, where each holding was resolved
from its deed rather than by geocoding its name. `parcel_by_apn` currently has
no caller and is kept deliberately: five holdings are still ungeocoded and this
is the tool for them.

PUBLIC-ENDPOINT ETIQUETTE. Nominatim and Overpass are volunteer-run and their
usage policies are binding, not advisory: Nominatim requires a real identifying
User-Agent and at most 1 request/second; Overpass asks for modest, bounded
queries. Overpass in particular returns "server too busy" often enough that
_overpass() retries across mirrors -- that is load-bearing behaviour, not
defensive padding. Results are also cached (see _overpass_cache_path), which
is as much courtesy to the endpoints as it is speed for us. If volume ever
grows past a handful of sites per run, self-hosting is the correct next step
rather than leaning harder on the public instances.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("vaulter.geo_providers")

# Identifies this project to Nominatim/Overpass per their usage policies.
USER_AGENT = "VaulterAI-Screening/1.0 (+https://github.com/YashuLanki/Vaulter_AI)"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# ── Overpass mirrors ────────────────────────────────────────────────────────
#
# Overpass' main public instance answers "too busy" often enough that a single
# endpoint is not usable, so the same query is tried against several mirrors.
#
# ORDER IS MEASURED, NOT ALPHABETICAL. Re-measured 2026-07-29 with the real
# 64-selector, 5-mile proximity query at five Arizona listings from
# data/drop/CostarExport.xlsx:
#
#     overpass.openstreetmap.fr  6.0s / 10.4s   correct element counts
#     overpass-api.de            26.2-31.9s     1 of 5 attempts; else 504/429
#     overpass.kumi.systems      ReadTimeout    0 of 5, even on /api/status
#     overpass.private.coffee    ReadTimeout    0 of 5, even on /api/status
#
# openstreetmap.fr is put first purely on that measurement: it answered every
# probe, 3-5x faster than the canonical instance, with an identical element
# set. The canonical instance stays second because it is the reference
# implementation and the most likely to be correct if the two ever disagree.
# kumi/private.coffee were unreachable from this network on every probe; they
# are kept last rather than dropped because one network's outage is not
# evidence they are dead everywhere, and `_HOST_COOLDOWN_S` means a run pays
# for their silence at most once.
#
# `trust_empty` -- whether an empty element list from this host is believed as
# a fact about the land. See `_overpass`. Only set it for a host verified to
# serve PLANET data, by the two-point probe described under OVERPASS_QUARANTINE.
_OVERPASS_MIRRORS_DECLARED = (
    ("overpass.openstreetmap.fr", "https://overpass.openstreetmap.fr/api/interpreter", True),
    ("overpass-api.de",           "https://overpass-api.de/api/interpreter",           True),
    ("overpass.kumi.systems",     "https://overpass.kumi.systems/api/interpreter",     True),
    ("overpass.private.coffee",   "https://overpass.private.coffee/api/interpreter",   True),
)

# NOT IN THE ROTATION, and recorded here so nobody re-adds it.
#
# overpass.osm.ch is a SWITZERLAND-ONLY extract. It was previously in the
# mirror list, where it caused a real wrong answer, and the cause was
# misdiagnosed twice as random overload. It is neither random nor overload.
# Measured 2026-07-29, one trivial `nwr["highway"](around:1000,...)` query:
#
#     osm.ch @ Avondale, Arizona   ->  0 elements, HTTP 200, 0.6s, no "remark"
#     osm.ch @ Bern, Switzerland   -> 20 elements, HTTP 200, 1.1s
#
# So it returns a confident, fast, structurally valid HTTP 200 with zero
# results for EVERY query in this firm's entire operating area, and it is by
# far the fastest host, so whenever the real mirrors were slow its empty won
# the race. That is what produced one parcel reading 0, 21, 21, 0 across four
# runs, and an in-town Avondale listing reporting "0 results" when the true
# answer is >=800 features within 5 miles (measured; the query's own `out 800`
# cap truncates it).
#
# The general lesson, which the `trust_empty` flag above exists to enforce:
# a regional extract is indistinguishable from a working planet mirror on any
# single query that legitimately has no results. Before adding ANY mirror,
# probe it at a point in your area of interest AND a point in the mirror's own
# country. If it answers the second and not the first, it is an extract.
OVERPASS_QUARANTINE = {
    "overpass.osm.ch": "Switzerland-only extract; returns 0 elements for all US queries",
}

# The quarantine ENFORCES itself rather than just documenting itself: a
# quarantined host put back into the list above is filtered straight out again.
# A comment saying "do not re-add this" is only as good as whoever reads it,
# and this particular mistake has already been made and misdiagnosed twice.
OVERPASS_MIRRORS = tuple(m for m in _OVERPASS_MIRRORS_DECLARED
                         if m[0] not in OVERPASS_QUARANTINE)

OPENTOPO_URL = "https://api.opentopodata.org/v1/ned10m"
PLANETARY_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"

_last_nominatim_call = 0.0

# ── Overpass tuning ─────────────────────────────────────────────────────────
# Read timeout. Every successful real proximity query measured on 2026-07-29
# landed between 6.0s and 31.9s, and the query itself declares [timeout:90]
# server-side, so 60s sits above every observed success and below the server's
# own budget. The previous 45s was under Overpass' own limit, so a slow-but-
# working host could be abandoned by us while it was still computing an answer.
_OVERPASS_READ_TIMEOUT = 60
_OVERPASS_CONNECT_TIMEOUT = 8
# Whole-call ceiling across every mirror and pass. Without it the worst case is
# 4 hosts x 2 passes x 60s = 8 minutes of a Claude Desktop tool call hanging.
_OVERPASS_DEADLINE_S = 150
_OVERPASS_PASSES = 2
# Once a host has failed in this process, skip it for this long. Two dead
# mirrors cost 45s each on the measured baseline, twice each; that is 180s of
# pure waiting per call, repaid on every listing in a multi-listing session.
_HOST_COOLDOWN_S = 600
_host_down_until: dict[str, float] = {}

# Cache format version. BUMP THIS whenever the meaning of a cached payload
# changes -- including when a bug in what we store or trust is fixed. A cached
# wrong value has already outlived its bug once here and read as MORE credible
# for being cached, so the version is the only thing standing between a fixed
# bug and its stale results. v1 is the first cache; entries written before the
# osm.ch quarantine never existed, so there is nothing to invalidate yet.
_OVERPASS_CACHE_VERSION = 1
# A WEEK IS A JUDGEMENT, NOT A MEASUREMENT. POI geography moves slowly, so a
# week-old answer is still a fair description of what is near a parcel; a week
# is short enough that anything wrong ages out fast. A miss now costs ~10s, so
# there is no reason to stretch it. Age travels back with the result
# (diag["cache_age_s"]) so a caller can say how old it is instead of implying
# it was fetched just now.
_OVERPASS_CACHE_TTL_S = 7 * 24 * 3600


def _get_json(url: str, params: dict, attempts: int = 2, timeout: int = 25) -> dict | None:
    """Single-endpoint JSON GET with one retry.

    Every provider here is a free public service, and all of them blip: a
    FEMA call that succeeded on a direct retest had failed moments earlier
    inside a real Phase 4 run. One retry turns those transient failures into
    non-events. Returns None if the request never succeeded, so callers can
    distinguish "provider unreachable" from "provider says no data" -- the
    two mean very different things about a parcel."""
    for attempt in range(attempts):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                # FEMA's ArcGIS endpoint signals failure IN BAND: HTTP 200
                # with {"error": {...}} in the body. Without this check the
                # retry above never fires for its most common failure mode,
                # which is exactly how a working query intermittently
                # reported "unavailable". None of the other providers here
                # use a top-level "error" key on success.
                if not (isinstance(data, dict) and "error" in data):
                    return data
        except (requests.RequestException, ValueError):
            pass
        if attempt < attempts - 1:
            time.sleep(1.5)
    return None


def _overpass_cache_path(query: str) -> Path | None:
    """Where a cached Overpass payload for `query` lives, or None if there is
    nowhere safe to put one.

    Shares SHARED_DIR/geo_cache with report.py's basemap cache, so one person
    fetching a listing's surroundings spares the whole team the round trip.
    config is imported lazily: it prints to stdout on some failure paths, and
    stdout is the MCP transport."""
    try:
        from config import SHARED_DIR, GEO_CACHE_DIR
        d = GEO_CACHE_DIR
        d.mkdir(parents=True, exist_ok=True)
    except Exception as e:                       # no shared folder, read-only, ...
        log.debug(f"Overpass cache unavailable: {e}")
        return None
    digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:24]
    return d / f"overpass_v{_OVERPASS_CACHE_VERSION}_{digest}.json"


def _overpass_cache_read(query: str) -> tuple[dict, dict] | None:
    """Returns (payload, meta) for a fresh cache hit, else None.

    The version is in the FILENAME, so bumping `_OVERPASS_CACHE_VERSION`
    orphans every older entry rather than reinterpreting it. The version
    inside the file is re-checked anyway, in case a path is ever constructed
    some other way."""
    path = _overpass_cache_path(query)
    if path is None or not path.exists():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if blob.get("version") != _OVERPASS_CACHE_VERSION:
        return None
    age = time.time() - blob.get("fetched_at", 0)
    if age < 0 or age > _OVERPASS_CACHE_TTL_S:
        return None
    payload = blob.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload, {"host": blob.get("host"), "age_s": int(age)}


def _overpass_cache_write(query: str, payload: dict, host: str) -> None:
    """Caches a successful, NON-EMPTY response.

    Empties are deliberately never cached. An empty is the one answer this
    module has historically got wrong, it is cheap to re-fetch, and caching it
    would give the least trustworthy result the longest life."""
    path = _overpass_cache_path(query)
    if path is None or not payload.get("elements"):
        return
    blob = {"version": _OVERPASS_CACHE_VERSION, "fetched_at": time.time(),
            "host": host, "payload": payload}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)                    # atomic; SHARED_DIR is shared
    except OSError as e:
        log.debug(f"Could not cache Overpass result: {e}")
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    _overpass_cache_prune(path.parent)


def _overpass_cache_prune(cache_dir: Path) -> None:
    """Drops expired entries this module wrote.

    Unlike report.py's basemap cache, which is keyed on a rounded bounding box
    and so has few possible keys, this is keyed on exact coordinates: one entry
    per listing, up to 550 KB each, in a folder that syncs to everyone's
    OneDrive. Left alone it only grows. Touches nothing but our own expired
    files -- older cache VERSIONS are matched too, since a version bump orphans
    them by design and nothing will ever read them again."""
    cutoff = time.time() - _OVERPASS_CACHE_TTL_S
    try:
        for f in cache_dir.glob("overpass_v*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass                              # in use by another instance
    except OSError as e:
        log.debug(f"Could not prune Overpass cache: {e}")


def _overpass_detailed(query: str, empty_is_suspect: bool = True,
                       use_cache: bool = True) -> tuple[dict | None, dict]:
    """Runs an Overpass query across the mirrors. Returns (data, diagnostics).

    `data` is None ONLY when no mirror produced a believable answer. That is
    "we do not know", never "there is nothing there" -- see below. `diagnostics`
    records what each host did, so a caller can tell the user *why* rather than
    just that something failed.

    WHY AN EMPTY RESULT IS TREATED SPECIALLY
    ----------------------------------------
    A mirror can return HTTP 200, valid JSON, and an EMPTY element list while
    being flatly wrong, and at the call site that is indistinguishable from a
    genuine "nothing here". It produced a real wrong answer: an Avondale
    listing reported 0 features within 5 miles when the true count is >=800.

    There are two separate causes, and only one of them is self-announcing.

    1. A server-side runtime failure DOES set a top-level "remark" (handled
       below). Reproducible on demand with a tight [timeout:], and confirmed on
       both trusted mirrors.
    2. The Avondale answer had NO remark. The culprit was overpass.osm.ch, and
       the cause was not random overload -- it is a Switzerland-only extract,
       so it is fast, confident and empty for every US query forever. Nothing
       in the response distinguishes it from the truth. It is now quarantined
       out of the rotation (see OVERPASS_QUARANTINE), which kills that
       specific bug; `trust_empty` is what guards against the next one.

    `empty_is_suspect` remains, and now DEFAULTS TO TRUE, because the class of
    bug outlives the instance: the next mirror someone adds could be another
    extract, and the failure is silent. So an empty counts as evidence only
    from a host explicitly flagged `trust_empty`, and even then only after the
    other mirrors have been given a chance to contradict it. An empty from an
    untrusted host yields None -- which callers render as UNAVAILABLE rather
    than as a finding about the land. "No amenities nearby" is a claim about a
    parcel; "a mirror shrugged" is not, and only one of them may reach a
    verdict.

    ORDERING AND RETRIES are measured, not guessed. Retrying the SAME host
    immediately after a 504 measured 504 again every time, so this makes a full
    pass over all hosts before retrying any of them, and a host that fails is
    benched for `_HOST_COOLDOWN_S`. The whole call is bounded by
    `_OVERPASS_DEADLINE_S`.

    Uses GET, which proved more reliable than POST against these hosts."""
    diag: dict = {"cached": False, "host": None, "attempts": [], "deadline_hit": False}

    if use_cache:
        hit = _overpass_cache_read(query)
        if hit is not None:
            payload, meta = hit
            diag.update(cached=True, host=meta["host"], cache_age_s=meta["age_s"])
            return payload, diag

    started = time.monotonic()
    trusted_empty: dict | None = None
    trusted_empty_host: str | None = None
    empty_agreed: set[str] = set()      # trusted hosts that each said "nothing"

    # Never refuse on bookkeeping alone. If every host happens to be benched,
    # a 30-second blip would otherwise turn into 10 minutes of UNAVAILABLE on
    # every listing in the session. Clear the bench and let them prove it.
    if all(_host_down_until.get(n, 0) > started for n, _, _ in OVERPASS_MIRRORS):
        _host_down_until.clear()

    for pass_no in range(_OVERPASS_PASSES):
        for name, url, trust_empty in OVERPASS_MIRRORS:
            if time.monotonic() - started > _OVERPASS_DEADLINE_S:
                diag["deadline_hit"] = True
                break
            if _host_down_until.get(name, 0) > time.monotonic():
                diag["attempts"].append({"host": name, "pass": pass_no,
                                         "outcome": "skipped (cooling down)"})
                continue

            t0 = time.monotonic()
            outcome = None
            try:
                resp = requests.get(
                    url,
                    params={"data": query},
                    headers={"User-Agent": USER_AGENT},
                    timeout=(_OVERPASS_CONNECT_TIMEOUT, _OVERPASS_READ_TIMEOUT),
                )
                if resp.status_code == 200 and resp.headers.get(
                        "content-type", "").startswith("application/json"):
                    data = resp.json()
                    n = len(data.get("elements") or [])
                    remark = data.get("remark")
                    if remark:
                        # Overpass signals runtime failure IN BAND, exactly as
                        # FEMA does in _get_json: HTTP 200, valid JSON, an
                        # EMPTY element list, and a top-level "remark". Induced
                        # and confirmed 2026-07-29 on BOTH trusted mirrors --
                        #   remark='runtime error: Query timed out in "query"
                        #           at line 1 after 2 seconds.'  elements=0
                        # Without this the empty is indistinguishable from
                        # "nothing near this parcel", and it comes from a host
                        # flagged trust_empty, so it would be believed and
                        # reported as a finding. A remark also marks merely
                        # PARTIAL results, which understate what is nearby, so
                        # neither case is returned or cached: an unreliable
                        # answer abstains rather than votes.
                        outcome = f"in-band remark, discarded: {str(remark)[:80]}"
                        _host_down_until[name] = time.monotonic() + _HOST_COOLDOWN_S
                    elif empty_is_suspect and n == 0:
                        if trust_empty:
                            # Believable, but keep looking: a contradicting
                            # non-empty from any host beats it outright.
                            trusted_empty, trusted_empty_host = data, name
                            empty_agreed.add(name)
                            outcome = "empty (trusted, seeking corroboration)"
                            if len(empty_agreed) >= 2:
                                # Two independent planet mirrors agree there is
                                # nothing here. Further asking cannot change the
                                # answer, and without this stop a genuinely
                                # empty location runs to the full deadline --
                                # measured at 150.7s before this, because the
                                # two dark mirrors still had to time out.
                                outcome = "empty (corroborated by 2 mirrors)"
                                diag["attempts"].append({
                                    "host": name, "pass": pass_no,
                                    "seconds": round(time.monotonic() - t0, 1),
                                    "outcome": outcome})
                                diag["host"] = name
                                diag["empty_trusted"] = True
                                return data, diag
                        else:
                            outcome = "empty (NOT trusted, ignored)"
                    else:
                        diag["attempts"].append({
                            "host": name, "pass": pass_no, "elements": n,
                            "seconds": round(time.monotonic() - t0, 1),
                            "outcome": "ok"})
                        diag["host"] = name
                        _overpass_cache_write(query, data, name)
                        return data, diag
                else:
                    # 429 = we are rate limited, 504 = the instance is
                    # overloaded. Both mean "not this host, not now".
                    outcome = f"HTTP {resp.status_code}"
                    _host_down_until[name] = time.monotonic() + _HOST_COOLDOWN_S
            except (requests.RequestException, ValueError) as e:
                outcome = type(e).__name__
                _host_down_until[name] = time.monotonic() + _HOST_COOLDOWN_S

            diag["attempts"].append({"host": name, "pass": pass_no,
                                     "seconds": round(time.monotonic() - t0, 1),
                                     "outcome": outcome})
        if diag["deadline_hit"]:
            break
        if pass_no < _OVERPASS_PASSES - 1:
            time.sleep(2)

    if trusted_empty is not None:
        # Nothing contradicted it, and it came from a host verified to serve
        # planet data. That is a finding about the land.
        diag["host"] = trusted_empty_host
        diag["empty_trusted"] = True
        return trusted_empty, diag
    return None, diag


def _overpass(query: str, attempts_per_host: int = 2,
              empty_is_suspect: bool = True) -> dict | None:
    """Back-compatible wrapper: the payload, or None if nothing believable came
    back. Prefer `_overpass_detailed` in new code -- it also returns WHICH host
    answered and how each one failed, which is what lets a caller distinguish
    "Overpass was busy" from "there is nothing near this parcel" in what it
    shows the user.

    `attempts_per_host` is accepted and ignored; retry policy is now
    `_OVERPASS_PASSES` round-robin passes over the mirrors, because retrying a
    just-failed host immediately measured as useless (504 then 504, every
    time)."""
    data, _ = _overpass_detailed(query, empty_is_suspect=empty_is_suspect)
    return data


def elevation_profile(lat: float, lng: float) -> dict:
    """Samples 5 points (center + 4 offsets ~500m away) to estimate terrain
    roughness -- same sampling pattern and meaning as the Google version:
    a PROXY for slope, not true parcel-boundary grading data.

    Uses the USGS NED 10m dataset, which is higher resolution than Google's
    elevation service but covers the US only. Out-of-coverage points come
    back as null rather than an error, so nulls are filtered before the
    spread is computed."""
    offset = 0.0045  # ~500m at these latitudes
    points = [
        (lat, lng),
        (lat + offset, lng), (lat - offset, lng),
        (lat, lng + offset), (lat, lng - offset),
    ]
    locations = "|".join(f"{p[0]},{p[1]}" for p in points)

    data = _get_json(OPENTOPO_URL, {"locations": locations})
    if data is None:
        return {"status": "UNAVAILABLE", "elevations": [], "max_diff_m": None}

    if data.get("status") != "OK":
        return {"status": data.get("status", "ERROR"), "elevations": [], "max_diff_m": None}

    elevations = [r["elevation"] for r in data.get("results", [])
                  if r.get("elevation") is not None]
    if not elevations:
        # Outside NED coverage (e.g. non-US). Not an error, just no data.
        return {"status": "NO_COVERAGE", "elevations": [], "max_diff_m": None}

    return {
        "status": "OK",
        "elevations": [round(e, 1) for e in elevations],
        "max_diff_m": round(max(elevations) - min(elevations), 1),
    }


def satellite_image(lat: float, lng: float) -> dict:
    """Aerial image centred on the listing, for visual inspection of
    whether the land is genuinely vacant.

    Uses NAIP via the Planetary Computer STAC API -- 30cm/pixel US aerial
    imagery, notably sharper than Google's static satellite tiles, and
    keyless. Two calls: STAC search for the covering scene, then fetch its
    pre-rendered PNG preview (which needs no SAS token, unlike the raw
    GeoTIFF asset)."""
    pad = 0.005  # ~500m box around the point
    bbox = f"{lng - pad},{lat - pad},{lng + pad},{lat + pad}"

    try:
        search = requests.get(
            PLANETARY_STAC_URL,
            params={"collections": "naip", "bbox": bbox, "limit": 1},
            timeout=30,
        )
        features = search.json().get("features", [])
    except (requests.RequestException, ValueError):
        return {"status": "ERROR", "image_bytes": None}

    if not features:
        return {"status": "NO_IMAGERY", "image_bytes": None}

    preview = features[0].get("assets", {}).get("rendered_preview", {}).get("href")
    if not preview:
        return {"status": "NO_IMAGERY", "image_bytes": None}

    try:
        img = requests.get(preview, timeout=45)
    except requests.RequestException:
        return {"status": "ERROR", "image_bytes": None}

    if img.status_code != 200 or not img.headers.get("content-type", "").startswith("image/"):
        return {"status": "ERROR", "image_bytes": None}

    return {
        "status": "OK",
        "image_bytes": img.content,
        "captured": features[0].get("properties", {}).get("datetime"),
    }


def geocode(query: str) -> dict:
    """Address/place -> coordinates, via Nominatim.

    Nominatim's usage policy caps this at 1 request/second and requires an
    identifying User-Agent; both are enforced here rather than left to the
    caller, since violating either can get the whole organisation blocked."""
    global _last_nominatim_call

    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _last_nominatim_call = time.monotonic()

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        results = resp.json()
    except (requests.RequestException, ValueError):
        return {"status": "ERROR", "lat": None, "lng": None, "label": None}

    if not results:
        return {"status": "ZERO_RESULTS", "lat": None, "lng": None, "label": None}

    hit = results[0]
    return {
        "status": "OK",
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "label": hit.get("display_name"),
    }


# Principal meridian codes as used in the BLM PLSS dataset's PLSSID field.
#
# These were VERIFIED empirically against the live service, not taken from a
# published table -- the obvious published orderings do not match what this
# dataset actually uses, and a wrong code silently returns a real section in
# the wrong part of the country. Querying T9N R12W Sec 19 with the meridian
# wildcarded returned two hits: code 27 gave Rosamond in Kern County (correct)
# and code 21 gave a point 250 miles north on the Sonoma coast. Both are valid
# sections; only one is the right property.
#
# So: when adding a state, verify the code by wildcarding the meridian for a
# township whose real location you already know, rather than trusting a table.
PLSS_MERIDIANS = {
    "gila and salt river": "14", "gila & salt river": "14",    # Arizona -- verified
    "san bernardino": "27",                                     # California -- verified
    "sixth principal": "06", "6th principal": "06",             # Colorado -- verified
    "6th p.m.": "06", "sixth p.m.": "06",
    "mount diablo": "21",                                       # N. California, Nevada
}

PLSS_SECTION_URL = ("https://gis.blm.gov/arcgis/rest/services/Cadastral"
                    "/BLM_Natl_PLSS_CadNSDI/MapServer/2/query")


def plss_section_centroid(state: str, meridian: str, township: int, township_dir: str,
                          range_: int, range_dir: str, section: int) -> dict:
    """Resolves a Public Land Survey System legal description to coordinates.

    Deeds, title policies and ALTA surveys almost never state a street address
    for raw land -- they state a legal description like "Section 18, Township
    11 South, Range 11 East, Gila and Salt River Base and Meridian". This
    turns that into a point, using the BLM's national PLSS dataset (free, no
    key). A section is one square mile, so the centroid is accurate to within
    about half a mile -- ample for a 5-mile proximity search, and far better
    than geocoding a property's nickname.

    IMPORTANT -- Texas is not PLSS land. Texas uses original land grants and
    abstract surveys instead, so this cannot resolve Texas properties; they
    need an address or APN. Louisiana and the original colonies are likewise
    outside PLSS.

    Returns the same {status, lat, lng, label} shape as geocode()."""
    key = meridian.strip().lower().replace(" base and meridian", "").replace(" meridian", "")
    mer_code = PLSS_MERIDIANS.get(key)
    if not mer_code:
        return {"status": "UNKNOWN_MERIDIAN", "lat": None, "lng": None, "label": meridian}

    # PLSSID packs state, meridian, township and range into a fixed-width id:
    # 2-char state, 2-digit meridian, 3-digit township + 1-digit fraction,
    # direction, then the same for range, then a trailing fraction digit.
    plss_id = (f"{state.upper()}{mer_code}"
               f"{township:03d}0{township_dir.upper()}"
               f"{range_:03d}0{range_dir.upper()}0")

    # FRSTDIVNO is a zero-padded 2-char STRING in this dataset ('02', not '2').
    # Querying an unpadded single-digit section silently returns nothing, which
    # looks identical to "this section does not exist".
    data = _get_json(PLSS_SECTION_URL, {
        "where": f"PLSSID='{plss_id}' AND FRSTDIVNO='{section:02d}'",
        "outFields": "PLSSID,FRSTDIVNO",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }, timeout=45)

    if data is None:
        return {"status": "UNAVAILABLE", "lat": None, "lng": None, "label": plss_id}

    features = data.get("features", [])
    if not features:
        return {"status": "NOT_FOUND", "lat": None, "lng": None, "label": plss_id}

    rings = features[0].get("geometry", {}).get("rings")
    if not rings:
        return {"status": "NO_GEOMETRY", "lat": None, "lng": None, "label": plss_id}

    ring = rings[0]
    return {
        "status": "OK",
        "lat": sum(p[1] for p in ring) / len(ring),
        "lng": sum(p[0] for p in ring) / len(ring),
        "label": f"Sec {section}, T{township}{township_dir}, R{range_}{range_dir} ({plss_id})",
    }


# CAL FIRE's statewide parcel layer -- keyless, ~13.1M parcels, all 58
# California counties in one service. Preferred over per-county assessor
# endpoints because it is a single integration rather than 58.
CA_PARCEL_URL = ("https://bz1uwwpkuinzbk94.svcs5.arcgis.com/bz1uwWPKUInZBK94/arcgis/rest"
                 "/services/CA_Statewide_Parcels_Public_view/FeatureServer/0/query")


def normalize_apn(apn: str) -> str:
    """Recorded-document APN -> the form the parcel GIS expects.

    Deeds write APNs with dashes and often a trailing check digit or suffix
    ('534-183-014-1', '0394-161-11-0-000'); the GIS layer stores a bare
    9-digit string ('534183014', '039416111'). Leading zeros are significant
    and must be kept."""
    digits = "".join(c for c in apn if c.isdigit())
    return digits[:9]


def parcel_by_apn(apn: str, county: str) -> dict:
    """Resolves a California APN to coordinates via the statewide parcel layer.

    Needed because a large share of California land is described by
    subdivision lot/tract ("Lot 5 of Tract 12345", "Block 16 of Banning
    Colony Lands") rather than by PLSS section/township/range, so
    plss_section_centroid() has nothing to work with. The APN is then the
    only precise identifier the deed provides.

    `county` is REQUIRED and load-bearing, not decoration: 9-digit APNs are
    NOT unique across California's 58 counties (prefix '03941' alone appears
    in San Bernardino, Lake, Yolo, Kern and Inyo). Omitting it can return a
    real parcel in the wrong county with nothing to signal the error.

    Returns the same {status, lat, lng, label} shape as geocode()."""
    normalized = normalize_apn(apn)
    data = _get_json(CA_PARCEL_URL, {
        "where": f"PARCEL_APN='{normalized}' AND COUNTYNAME='{county.upper()}'",
        "outFields": "PARCEL_APN,COUNTYNAME,SITE_CITY",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json",
    }, timeout=60)

    if data is None:
        return {"status": "UNAVAILABLE", "lat": None, "lng": None, "label": normalized}

    features = data.get("features", [])
    if not features:
        return {"status": "NOT_FOUND", "lat": None, "lng": None, "label": normalized}

    rings = features[0].get("geometry", {}).get("rings")
    if not rings:
        return {"status": "NO_GEOMETRY", "lat": None, "lng": None, "label": normalized}

    ring = rings[0]
    attrs = features[0].get("attributes", {})
    return {
        "status": "OK",
        "lat": sum(p[1] for p in ring) / len(ring),
        "lng": sum(p[0] for p in ring) / len(ring),
        "label": f"APN {normalized}, {attrs.get('SITE_CITY') or county}",
        "city": attrs.get("SITE_CITY"),
    }


