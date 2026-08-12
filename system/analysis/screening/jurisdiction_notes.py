"""
jurisdiction_notes.py
---------------------
Attach a city's own researched dossier to every listing in that city.

Why this exists: the screener ranks a listing by how it compares to the
firm's own holdings, its pricing against local peers, and how distressed it
looks. None of that can answer the question a partner actually asks next --
*is this jurisdiction going anywhere?* A parcel in a city whose water
constraint just lifted, whose capital plan is funded, and whose impact fees
just moved is a materially different proposition from an identical parcel in
a stagnant one, and nothing in a CoStar export says so.

The dossiers already existed (built by the `vaulter-city-researcher`
subagent) and were read by NOTHING -- one sat in `docs/jurisdictions/` for
weeks with a section literally titled "What this changes about screening
<city> listings". This module is the wire that was missing.

Three rules, matching the ones the rest of this package already follows:

  * **Informational only.** This NEVER touches Fit_Score, Fit_Tier, or any
    ranking input -- exactly like `Cautions` and `Portfolio_Comparison`. A
    dossier is research a human wrote; letting it move a score would turn
    prose into arithmetic, which is the mistake `passed_on_patterns.py`
    exists to avoid. `check_screener.py` asserts the score is byte-identical
    with and without dossiers present.
  * **Silence when there is no dossier.** Most cities in any export have
    none -- the real 216-row file spans 30 cities and one dossier existed.
    A listing with no dossier gets an empty string, never a guess and never
    a placeholder implying the city was assessed.
  * **The team's copy, not the maintainer's.** Dossiers live in the shared
    folder beside the property summaries, so every teammate has the same
    research. `docs/` is deliberately never shipped, which is exactly why
    the original dossier could never have reached anyone else.
"""

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The heading a dossier uses for its screening-relevant conclusions. Written
# by the city-researcher agent, and the only part quoted into a workbook --
# a dossier is thousands of words of sourced research, of which this is the
# part that bears on a purchase decision.
_SCREENING_HEADING = re.compile(r"^#+\s*\d*\.?\s*What this changes about screening",
                                re.IGNORECASE)

# Trailing detail that is real research but not a screening signal.
_STOP_HEADING = re.compile(r"^#+\s*\d*\.?\s*(Open questions|Sources|Gaps)", re.IGNORECASE)

_MAX_NOTE_CHARS = 700


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower().strip()).strip("-")


# A postal code is NOT a prefix of the state's name, and treating it as one is
# actively dangerous: "arizona" starts with "ar", which is ARKANSAS. An earlier
# version of this file matched states by prefix and would have handed Arkansas
# listings Arizona's water research. Measured 2026-08-12. Exact codes only.
_STATE_CODES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new-hampshire": "nh", "new-jersey": "nj",
    "new-mexico": "nm", "new-york": "ny", "north-carolina": "nc",
    "north-dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode-island": "ri", "south-carolina": "sc",
    "south-dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west-virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district-of-columbia": "dc",
}


def _state_code(value: str) -> str:
    """Two-letter code for a state written either way, or "" if unrecognised."""
    v = _slug(value)
    if not v:
        return ""
    if len(v) == 2:
        return v
    return _STATE_CODES.get(v, "")


def jurisdictions_dir() -> Path | None:
    """Where the team's dossiers live, or None if the shared folder is absent."""
    try:
        from config import SHARED_DIR
        return Path(SHARED_DIR) / "jurisdictions"
    except Exception:
        return None


def load_dossiers() -> dict:
    """
    {(city_slug, state_slug): note_text} for every dossier on this machine.

    Read once per screening run rather than once per row. Returns {} when the
    folder is missing, which is the normal state on a machine where nobody
    has written a dossier yet -- not an error.
    """
    folder = jurisdictions_dir()
    if not folder or not folder.is_dir():
        return {}

    out = {}
    for path in sorted(folder.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning(f"[JURISDICTION] Could not read {path.name}: {e}")
            continue
        note = _screening_section(text)
        if not note:
            continue
        # Filename is "<city>-<state>.md", e.g. "casa-grande-az.md". The state
        # is the LAST hyphen-separated piece so multi-word cities keep working.
        stem = path.stem.lower()
        if "-" not in stem:
            continue
        city, _, state = stem.rpartition("-")
        out[(city, state)] = note
    return out


def _screening_section(text: str) -> str:
    """
    The dossier's own "what this changes about screening" conclusions, trimmed.

    Deliberately quotes the section a human wrote for this purpose rather than
    summarising the whole dossier: a summary of research is a new claim, and
    this package's whole discipline is that nothing invents a signal. If a
    dossier has no such section it contributes nothing, which is honest --
    background research that never reached a conclusion should not appear
    beside a ranking as though it had.
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if _SCREENING_HEADING.match(ln)), None)
    if start is None:
        return ""

    body = []
    for ln in lines[start + 1:]:
        if _STOP_HEADING.match(ln) or (ln.startswith("#") and len(body) > 2):
            break
        body.append(ln)

    # Collapse to a single readable line: a spreadsheet cell, not a document.
    flat = " ".join(l.strip() for l in body if l.strip())
    flat = re.sub(r"\*\*|\*|`", "", flat)
    flat = re.sub(r"^\d+\.\s*", "", flat)
    flat = re.sub(r"\s+(\d+)\.\s+", r" | ", flat)
    flat = re.sub(r"\s{2,}", " ", flat).strip()
    if len(flat) > _MAX_NOTE_CHARS:
        flat = flat[:_MAX_NOTE_CHARS].rsplit(" ", 1)[0] + " ..."
    return flat


def note_for(city: str, state: str, dossiers: dict) -> str:
    """
    The dossier note for one listing's city, or "" when none exists.

    Matching is on city AND state so a common city name (there is a Buckeye
    in more than one state) can never pick up the wrong jurisdiction's
    research -- the same reason the property registry matches canonically
    rather than by substring.
    """
    if not dossiers:
        return ""
    c, s = _slug(city), _slug(state)
    if not c:
        return ""
    if (c, s) in dossiers:
        return dossiers[(c, s)]

    # A code against a spelled-out name ("AZ" vs "Arizona") is a formatting
    # difference; a genuinely different state is not. Measured 2026-08-12: an
    # earlier version fell back to "the only dossier with this city name" and
    # handed Arizona's research to a listing in Coolidge, TEXAS. Same-named
    # towns exist in several states, and attaching the wrong jurisdiction's
    # water and impact-fee findings to a listing is exactly the confidently-
    # wrong output this package refuses to produce elsewhere.
    want = _state_code(state)
    if not want:
        return ""  # no state, or one we can't resolve: don't guess
    for (dc, ds), note in dossiers.items():
        if dc == c and _state_code(ds) == want:
            return note
    return ""
