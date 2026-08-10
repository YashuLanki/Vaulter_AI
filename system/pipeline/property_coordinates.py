"""
Stored latitude/longitude for Vaulter portfolio properties.

WHY THIS EXISTS. The proximity tool used to derive a property's location by
geocoding its name on every run. That was always fragile, and it broke
outright when the geocoder moved off Google: roughly 40% of the portfolio is
named after a street intersection ("<Road A> & <Road B>"), and OpenStreetMap's
Nominatim does not support intersection geocoding at all. Google happened to
guess at them; that was luck, not a design.

The real fix is not a better geocoder. These are the firm's OWN properties --
their locations are known facts that belong recorded once, not re-derived
from a folder name on every run. This module is that record.

WHY A SEPARATE FILE RATHER THAN A COLUMN IN THE PROJECT MASTER.
`data/project_master/Vaulter_Project_Master.csv` is a **Smartsheet export**.
Anything written into it is destroyed the next time somebody re-exports.
Coordinates therefore live in their own file, keyed by property name, and the
Project Master stays the read-only source of truth for which properties exist.

PROVENANCE MATTERS. Every entry records where its coordinate came from and
how precise it is, because the failure mode here is silent: a wrong
coordinate points a 5-mile proximity search at the wrong place and nothing
about the output looks wrong. `source` and `precision` are there so a reader
can tell a surveyed legal description apart from a guess at a town centre.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

log = logging.getLogger("vaulter.proximity")

COORDS_FILENAME = "property_coordinates.csv"

FIELDNAMES = [
    "property_name",   # must match the Project Master's "Project Name"
    "state",
    "latitude",
    "longitude",
    "address",         # the real address/legal description this came from
    "source",          # e.g. "title commitment p.3", "ALTA survey", "geocoded name"
    "precision",       # parcel | section | intersection | city -- see below
    "updated",
]

# How much to trust a coordinate:
#   parcel        -- from a deed, survey, APN or legal description, resolved to
#                    THIS parcel specifically. Trustworthy.
#   section       -- resolved only to the PLSS section (or equivalent block) the
#                    parcel sits in, so the point is the section centroid rather
#                    than the parcel's own. A section is one square mile, so the
#                    error is up to ~0.7 miles -- immaterial for a 5-mile radius
#                    search, but two parcels in the SAME section share one point
#                    and their proximity results will be byte-identical. Added
#                    2026-08-10: ten properties in four groups were labelled
#                    `parcel` while sharing a coordinate, which overstated what
#                    was actually known and made same-section properties look
#                    distinguishable when they aren't.
#   intersection  -- the named cross-streets were resolved. Good enough for a
#                    5-mile radius search.
#   city          -- only the town was identifiable. A proximity search from
#                    this is indicative, NOT accurate -- callers should say so.
PRECISION_LEVELS = ("parcel", "section", "intersection", "city")


def coords_path(data_dir: Path) -> Path:
    """
    The coordinates table: this machine's own copy if it has one, otherwise the
    team's shared "Smartsheet Portfolio" folder (added 2026-08-03, same reason
    as the Project Master -- a fresh install shipped with no coordinates at all,
    so run_proximity_for_property refused every single property by name).

    Falls back to the LOCAL path when neither exists, deliberately: a caller
    writing a new table should write it to their own machine, not silently
    into the folder the whole team reads.
    """
    local = data_dir / "project_master" / COORDS_FILENAME
    if local.exists():
        return local
    try:
        from config import SMARTSHEET_PORTFOLIO_DIR
        shared = SMARTSHEET_PORTFOLIO_DIR / COORDS_FILENAME
        if shared.exists():
            return shared
    except Exception:
        pass  # unreachable shared folder just means "use the local path"
    return local


def load_coordinates(data_dir: Path) -> dict:
    """Returns {property_name: {lat, lon, address, source, precision}}.

    Missing file is normal (nothing recorded yet) and returns {} rather than
    raising -- the caller falls back to geocoding."""
    path = coords_path(data_dir)
    if not path.exists():
        return {}

    out = {}
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                name = (row.get("property_name") or "").strip()
                lat, lon = (row.get("latitude") or "").strip(), (row.get("longitude") or "").strip()
                if not name or not lat or not lon:
                    continue
                try:
                    out[name] = {
                        "lat": float(lat),
                        "lon": float(lon),
                        "address": (row.get("address") or "").strip(),
                        "source": (row.get("source") or "").strip(),
                        "precision": (row.get("precision") or "").strip() or "unknown",
                    }
                except ValueError:
                    log.warning(f"[COORDS] Bad lat/lon for '{name}' -- skipping row")
    except OSError as e:
        log.warning(f"[COORDS] Could not read {path}: {e}")
        return {}
    return out


def lookup(data_dir: Path, property_name: str) -> dict | None:
    """Exact match first, then a single unambiguous case-insensitive
    substring match. Deliberately refuses to guess when several properties
    match -- picking one arbitrarily would put the search miles away with no
    visible sign anything went wrong."""
    coords = load_coordinates(data_dir)
    if property_name in coords:
        return coords[property_name]

    hits = [v for k, v in coords.items() if property_name.lower() in k.lower()]
    return hits[0] if len(hits) == 1 else None


def save_coordinate(data_dir: Path, property_name: str, state: str,
                    lat: float, lon: float, address: str = "",
                    source: str = "", precision: str = "city") -> None:
    """Adds or updates one property's coordinate, preserving every other row.

    Rewrites the whole file rather than appending, so re-running a lookup
    corrects a bad entry instead of silently adding a duplicate that
    `lookup()` would then refuse to resolve."""
    path = coords_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    rows = {}
    if path.exists():
        try:
            with open(path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    key = (row.get("property_name") or "").strip()
                    if key:
                        rows[key] = row
        except OSError as e:
            # Refuse to clobber a file we could not read -- rewriting from an
            # empty dict here would delete every other property's coordinate.
            raise OSError(f"Refusing to rewrite {path}: could not read it first ({e})")

    from datetime import datetime
    rows[property_name] = {
        "property_name": property_name,
        "state": state,
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "address": address,
        "source": source,
        "precision": precision if precision in PRECISION_LEVELS else "city",
        "updated": datetime.now().strftime("%Y-%m-%d"),
    }

    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        for key in sorted(rows):
            w.writerow(rows[key])
    tmp.replace(path)   # atomic -- a reader never sees a half-written file


# ── Merging coordinates into the Project Master ────────────────────────────

MERGED_COLUMNS = ["Latitude", "Longitude", "Location Precision", "Location Source"]


def merge_into_project_master(data_dir: Path, master_path: Path | None = None) -> dict:
    """Writes Latitude/Longitude columns into the Project Master CSV.

    RE-RUN THIS AFTER EVERY SMARTSHEET EXPORT. The Project Master is a
    Smartsheet export, so a fresh export overwrites these columns. That is
    why `property_coordinates.csv` -- not the Project Master -- stays the
    source of truth: this function is a projection of it, and is safe to run
    as many times as you like.

    Matching mirrors lookup(): exact name first, then a single unambiguous
    case-insensitive substring match. A property matching several coordinate
    rows is left blank rather than guessed at.

    Returns {"matched": n, "blank": n, "columns_added": bool}.
    """
    if master_path is None:
        candidates = sorted((data_dir / "project_master").glob("*.csv"))
        candidates = [c for c in candidates if c.name != COORDS_FILENAME]
        if not candidates:
            raise FileNotFoundError(f"No Project Master CSV found in {data_dir / 'project_master'}")
        master_path = candidates[0]

    coords = load_coordinates(data_dir)

    with open(master_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    added = [c for c in MERGED_COLUMNS if c not in fieldnames]
    fieldnames.extend(added)

    matched = 0
    for row in rows:
        name = (row.get("Project Name") or "").strip()
        hit = coords.get(name)
        if hit is None:
            partial = [v for k, v in coords.items() if name and name.lower() in k.lower()]
            hit = partial[0] if len(partial) == 1 else None

        if hit:
            row["Latitude"] = f"{hit['lat']:.6f}"
            row["Longitude"] = f"{hit['lon']:.6f}"
            row["Location Precision"] = hit["precision"]
            row["Location Source"] = hit["source"]
            matched += 1
        else:
            for c in MERGED_COLUMNS:
                row.setdefault(c, "")

    tmp = master_path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    tmp.replace(master_path)

    return {"matched": matched, "blank": len(rows) - matched, "columns_added": bool(added)}
