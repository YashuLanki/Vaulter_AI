"""
Compares a listing (or an off-market property) against the firm's own deal
history, and returns the most similar past deals with a plain-English reason
for each match and their documented Approach & Outcome.

Deliberately does NOT do market/price comparison -- that requires deciding
where peer pricing for a standalone property comes from (an open question,
on hold as of 2026-08-06). This module only compares deal CHARACTERISTICS
(location, land type, plan type, size) and reports HISTORY (how the firm
approached similar deals and what happened), never a price or a verdict.
"how did we approach deals like this, and what happened" -- not
"should we do this deal."

## Where the comparison data comes from, and why it isn't fully automatic

Every property now has a `## Approach & Outcome` section (see
Vaulter AI Shared/property_summaries/*.md), but classifying a deal's
land type, plan type, and outcome from that free-text prose is a judgment
call, not something a regex can reliably do -- exactly why fit_screen.py's own
docstring warns that name-based column matching alone misreads real exports.
So INDEX_PATH is built by having an agent read each summary and
tag it against the fixed category lists below (LAND_TYPES, PLAN_TYPES,
OUTCOME_STATUSES) -- a one-time (or periodic, after new summaries are added)
curation pass, not a push-button rebuild. What IS deterministic, tested, and
argue-with-able is everything downstream of that index: the scoring in
find_similar_deals() below.

## Comparable to the WEIGHTS caveat in fit_screen.py

fit_screen.py's own ASSUMPTIONS admits its four WEIGHTS have no evidence
behind them and need a partner's judgment, not another search. The scoring
weights below are in the same spirit: reasonable, arguable defaults, not a
measured result. Change them in ASSUMPTIONS, not scattered through the code.
"""

import json
import re
from pathlib import Path

from analysis.screening.market_eras import era_note

# ══════════════════════════════════════════════════════════════════
# Fixed category lists -- must match exactly what index-building
# agents are instructed to use. Adding a new value here means also
# updating the extraction instructions used to build the index.
# ══════════════════════════════════════════════════════════════════

LAND_TYPES = {"residential", "commercial", "industrial", "mixed-use", "agricultural", "unclear"}
# `acquire-finished-lots` added 2026-08-10, and it is not a cosmetic split.
# A blind re-read of the firm's own documents found that every property then
# filed as `hold-only` had in fact been bought as ALREADY-PLATTED or FINISHED
# lots -- the firm did no entitlement work because none was needed; the value-add
# was the acquisition itself (price, timing, a distressed seller). Filing that
# under a label that reads as "no plan" actively misled: ask the system "have we
# done a distressed finished-lot package before?" and it answered with deals
# that looked like the firm had done nothing. This is also one of the firm's
# most profitable documented patterns, so hiding it was expensive.
#
# `hold-only` is deliberately KEPT, for a genuine buy-raw-land-and-sit case.
# As of this writing no property in the portfolio is one -- worth knowing in
# itself.
PLAN_TYPES = {"rezone", "subdivide", "entitle-only", "annex", "hold-only",
              "acquire-finished-lots", "assemble-resell", "recapitalization",
              "unclear"}
OUTCOME_STATUSES = {"sold", "still-held", "pending-sale", "transferred-not-sold",
                     "pending-acquisition", "unclear"}

# Same acreage bands fit_screen.py uses for its own exit-comp logic (see
# _BANDS there) -- reused here so "similar size" means the same thing across
# both tools, not two different conventions a reader has to reconcile.
_SIZE_BANDS = [(20, "<20ac"), (100, "20-100ac"), (float("inf"), "100ac+")]
_BAND_ORDER = [label for _, label in _SIZE_BANDS]

ASSUMPTIONS = {
    "state_match_points": 3,
    # Only counted on top of a state match -- a matching county name with a
    # different state is either coincidence or bad data, never a real signal.
    "county_match_points": 2,
    "land_type_match_points": 3,
    "plan_type_match_points": 2,
    "size_band_same_points": 2,
    "size_band_adjacent_points": 1,
    # Below this combined score, the deal doesn't meaningfully resemble
    # anything in the portfolio. Reporting a low-score "closest" match would
    # look like a real comparison when it isn't -- same failure mode this
    # project has flagged before (a neutral floor beats a flattering guess).
    # Set above what a single soft signal can reach alone (e.g. plan_type (2)
    # + adjacent size (1) = 3) -- tested against a deliberately unrelated
    # deal (WY agricultural hold vs. this AZ/CA residential-heavy portfolio)
    # that scored exactly 3 on plan_type + loose size alone; 5 requires at
    # least one real anchor (state or land_type) plus something else, not
    # just a shared label and a rough size guess.
    "min_score_to_report": 5,
}

INDEX_PATH = Path(__file__).resolve().parents[2] / "data" / "portfolio_comparison_index.json"


def _size_band(acres) -> str | None:
    try:
        a = float(acres)
    except (TypeError, ValueError):
        return None
    # float("nan") passes the conversion above without raising -- and NaN
    # compares False against every limit below, including infinity, so the
    # generator would find nothing and raise StopIteration. A missing/blank
    # acreage is a normal input here (not every CoStar row has one), not an
    # error condition, so this must return None rather than crash the caller.
    if a != a:  # the standard, allocation-free way to test for NaN
        return None
    return next(label for limit, label in _SIZE_BANDS if a < limit)


def _band_distance(band_a: str, band_b: str) -> int:
    """0 = same band, 1 = adjacent, 2+ = far apart. Order follows _BAND_ORDER."""
    return abs(_BAND_ORDER.index(band_a) - _BAND_ORDER.index(band_b))


def load_index(path: Path = None) -> list[dict]:
    """
    Loads the portfolio comparison index. Returns [] (not an error) if the
    file doesn't exist yet -- callers should treat that as "no comparison
    data available" and say so, the same way an unevidenced market in
    fit_screen.py still ranks normally rather than raising.
    """
    p = path or INDEX_PATH
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _score(facts: dict, record: dict) -> tuple[int, list[str]]:
    """Returns (score, reasons) for how well `record` matches `facts`."""
    score = 0
    reasons = []

    f_state = (facts.get("state") or "").strip().upper()
    r_state = (record.get("state") or "").strip().upper()
    state_matched = bool(f_state) and f_state not in ("", "UNCLEAR") and f_state == r_state
    if state_matched:
        score += ASSUMPTIONS["state_match_points"]
        reasons.append(f"same state ({r_state})")

        f_county = (facts.get("county") or "").strip().lower()
        r_county = (record.get("county") or "").strip().lower()
        if f_county and f_county != "unclear" and f_county == r_county:
            score += ASSUMPTIONS["county_match_points"]
            reasons.append(f"same county ({record.get('county')})")

    f_land = (facts.get("land_type") or "").strip().lower()
    r_land = (record.get("land_type") or "").strip().lower()
    if f_land and f_land != "unclear" and f_land == r_land:
        score += ASSUMPTIONS["land_type_match_points"]
        reasons.append(f"same land type ({r_land})")

    f_plan = (facts.get("plan_type") or "").strip().lower()
    r_plan = (record.get("plan_type") or "").strip().lower()
    if f_plan and f_plan != "unclear" and f_plan == r_plan:
        score += ASSUMPTIONS["plan_type_match_points"]
        reasons.append(f"same approach ({r_plan})")

    f_band = _size_band(facts.get("acres"))
    r_band = _size_band(record.get("acres"))
    if f_band and r_band:
        dist = _band_distance(f_band, r_band)
        if dist == 0:
            score += ASSUMPTIONS["size_band_same_points"]
            reasons.append(f"similar size ({r_band})")
        elif dist == 1:
            score += ASSUMPTIONS["size_band_adjacent_points"]
            reasons.append(f"comparable size ({r_band} vs {f_band})")

    return score, reasons


# Land-type text -> this module's fixed vocabulary. Same pattern families as
# fit_screen.py's own _EXIT_PATH, since a CoStar listing's "Secondary Type"
# text is the same kind of free text either module has to classify -- but
# mixed-use is split out as its own category here (fit_screen.py folds it into
# commercial for exit-pricing purposes, which is right for pricing but would
# silently hide every real mixed-use deal in the portfolio index from ever
# matching a mixed-use listing).
_LAND_TYPE_PATTERNS = (
    ("mixed-use", r"mixed[\s-]*use"),
    ("residential", r"residential|single[\s-]*family|multi[\s-]*family|apartment|"
                     r"condo|townhome|manufactured\s*home"),
    ("industrial", r"industrial|warehouse|distribution|manufactur|truck\s*stop|storage\s*yard"),
    ("agricultural", r"agricultur|pasture|ranch\b|farm|timber|open\s*space"),
    ("commercial", r"commercial|retail|office|medical|health|restaurant|fast\s*food|hotel|"
                   r"store|service\s*station|auto|bank|car\s*wash"),
)


def classify_land_type(text) -> str:
    """
    Maps free-text land-use description (e.g. a CoStar "Secondary Type" or
    "Proposed Land Use" value) to this module's fixed LAND_TYPES vocabulary.
    Returns "unclear" for blank/unrecognized text -- never raises, since a
    listing with a blank or odd land-use field is normal input, not an error.
    """
    s = str(text or "").strip().lower()
    if not s or s == "nan":
        return "unclear"
    for label, pattern in _LAND_TYPE_PATTERNS:
        if re.search(pattern, s):
            return label
    return "unclear"


def compare_listing_row(state, county, land_type_text, acres, top_n: int = 3,
                         index: list[dict] = None) -> dict:
    """
    Same as find_similar_deals(), but for a single CoStar listing row: takes
    the listing's own raw State/County/land-use-text/acreage, classifies the
    land-use text into the fixed vocabulary, and compares. No plan_type is
    passed -- a listing the firm hasn't bought yet has no documented approach
    to match on, and guessing one would misrepresent an unmade decision as a
    known fact.
    """
    facts = {
        "state": state,
        "county": county,
        "land_type": classify_land_type(land_type_text),
        "acres": acres,
    }
    return find_similar_deals(facts, top_n=top_n, index=index)


# ─── Describing a match in one line ───────────────────────────────────────────
# The screening workbook and HTML report get ONE short string per listing, so
# whatever goes here has to earn its space. Before 2026-08-10 it was just
# "<name> (still held)" -- which told an analyst nothing: it could not
# distinguish an entitlement play from a finished-lot buy, and "still held"
# covered "never tried to sell", "marketed seven years unsuccessfully", and
# "investors already got their capital back" with the same two words.

_PLAN_PHRASE = {
    "subdivide":             "subdivided into lots",
    "rezone":                "rezoned",
    "entitle-only":          "entitled without subdividing",
    "annex":                 "annexed",
    "acquire-finished-lots": "bought already-finished lots",
    "hold-only":             "held, no value-add plan",
    "assemble-resell":       "assembled to resell",
    "recapitalization":      "recapitalized",
    "unclear":               "approach not established",
}

_OUTCOME_PHRASE = {
    "still-held":           "still held",
    "sold":                 "sold",
    "pending-sale":         "sale pending",
    "pending-acquisition":  "not yet owned",
    "transferred-not-sold": "transferred, not sold",
    "unclear":              "outcome unclear",
}

# The provenance markers written into notes by the 2026-08-10 verification pass.
# Stripped from the displayed note and turned into a compact flag instead.
_VERIFIED_MARK = re.compile(r"\[approach independently verified[^\]]*\]", re.I)
_UNVERIFIED_MARK = re.compile(r"\[[^\]]*not yet independently re-read[^\]]*\]", re.I)

# Prices must never reach the screening column. This tool compares
# characteristics and history, never price -- check_screener.py asserts it. A
# note is free text a human wrote, so strip defensively rather than trusting
# that no future note ever mentions a figure.
_PRICE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?\s*[MmKk]?")


def summarize_match(m: dict, note_chars: int = 95) -> str:
    """
    One compact line for a matched deal: what the firm did, how it turned out,
    whether that's independently verified, and the shortest useful slice of the
    note explaining it.

    Never raises and never emits a price -- a match with missing fields simply
    says less.
    """
    name = str(m.get("property_name") or "").strip() or "(unnamed)"
    plan = _PLAN_PHRASE.get((m.get("plan_type") or "").strip().lower())
    outcome = _OUTCOME_PHRASE.get((m.get("outcome_status") or "").strip().lower())

    note = str(m.get("notes") or "")
    verified = bool(_VERIFIED_MARK.search(note))
    note = _UNVERIFIED_MARK.sub("", _VERIFIED_MARK.sub("", note))
    note = _PRICE.sub("", note)
    # Removing a price mid-sentence leaves debris: "sold 2014 for $4M, ~2yr
    # hold" became "sold 2014 for , ~2yr hold". Tidy the orphaned preposition
    # and doubled punctuation rather than shipping a sentence with a hole in it.
    note = re.sub(r"\b(for|at|of)\s+(?=[,;.]|\s|$)", "", note)
    note = re.sub(r"\s*([,;])\s*(?=[,;])", "", note)
    note = re.sub(r"\s+([,;.])", r"\1", note)          # no space before punctuation
    note = re.sub(r"\s+", " ", note).strip(" ;,.-")

    if len(note) > note_chars:
        cut = note[:note_chars].rsplit(" ", 1)[0]
        note = cut.rstrip(" ;,.-") + "..."

    bits = [b for b in (plan, outcome) if b]
    head = f"{name} — {', '.join(bits)}" if bits else name
    if verified:
        head += " [verified]"
    return f"{head}: {note}" if note else head


def find_similar_deals(facts: dict, top_n: int = 5, index: list[dict] = None) -> dict:
    """
    Compares `facts` (a listing or off-market property's characteristics)
    against the firm's own deal history and returns the most similar past
    deals -- what they were, how the firm approached them, what happened.

    Args:
        facts: any of state, county, land_type, acres, plan_type. All
               optional -- missing fields simply can't contribute to a
               match, never an error. land_type/plan_type should be one of
               the fixed category values above if you want them to match;
               an unrecognized value just won't match anything (not a
               crash).
        top_n: maximum matches to return.
        index: pass a pre-loaded index to avoid re-reading the file
               repeatedly (e.g. when scoring every row of a CoStar export);
               omit to load fresh from INDEX_PATH.

    Returns:
        {
            "matches": [
                {"property_name", "filename", "score", "reasons": [...],
                 "outcome_status", "notes", "era_note"},
                ...
            ],
            "coverage_note": str,   # honest statement of index size / gaps
        }
        `matches` is [] with a plain-English coverage_note if nothing scores
        at or above min_score_to_report -- never a forced weak match.
    """
    data = index if index is not None else load_index()

    if not data:
        return {
            "matches": [],
            "coverage_note": ("No portfolio comparison data is available yet. The comparison "
                               "index hasn't been built from the property summaries."),
        }

    scored = []
    for record in data:
        score, reasons = _score(facts, record)
        if score >= ASSUMPTIONS["min_score_to_report"]:
            scored.append((score, reasons, record))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_n]

    matches = []
    for score, reasons, record in top:
        matches.append({
            "property_name": record.get("property_name", record.get("filename", "unknown")),
            "filename": record.get("filename"),
            "score": score,
            "reasons": reasons,
            # plan_type travels with the match so a caller can say what the
            # firm actually DID, not just what happened. Two deals can match on
            # geography and size and imply opposite lessons -- an entitlement
            # play vs a finished-lot buy -- and without this the caller cannot
            # tell them apart.
            "plan_type": record.get("plan_type", "unclear"),
            "outcome_status": record.get("outcome_status", "unclear"),
            "notes": record.get("notes", ""),
            "era_note": era_note(record.get("entry_year")),
        })

    if not matches:
        coverage_note = (f"No property in the portfolio's {len(data)}-deal comparison index "
                          f"closely resembles this one on location, land type, plan, or size. "
                          f"That's a real finding, not a gap -- this may be a genuinely new kind "
                          f"of deal for the firm.")
    else:
        coverage_note = f"Compared against {len(data)} portfolio deals with a comparison history."

    return {"matches": matches, "coverage_note": coverage_note}
