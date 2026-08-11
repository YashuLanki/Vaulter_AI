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

INDEX_FILENAME = "portfolio_comparison_index.json"
INDEX_PATH = Path(__file__).resolve().parents[2] / "data" / INDEX_FILENAME


def index_path() -> Path:
    """
    This machine's own copy if it has one, otherwise the team's shared
    "Smartsheet Portfolio" folder -- same local-then-shared pattern as
    property_coordinates.coords_path(), and for the same reason. Added
    2026-08-11: this index has no shared-folder path at all until now, so a
    freshly-installed teammate had no way to ever receive it (nothing rebuilds
    it automatically -- it's agent-curated, not derived from a formula), and
    an existing local copy simply goes stale forever with no update path. The
    live install on THIS machine was found 5 days stale (Aug 6) while a
    same-day re-verification pass had just moved 44 of 49 records from
    unsourced to document-cited -- a gap this closes going forward.

    Falls back to the LOCAL path when neither exists, deliberately: a caller
    building a new index writes it to their own machine first, not silently
    into the folder the whole team reads -- publishing to the team is a
    separate, deliberate step (see publish_index() below).
    """
    if INDEX_PATH.exists():
        return INDEX_PATH
    try:
        from config import SMARTSHEET_PORTFOLIO_DIR
        shared = SMARTSHEET_PORTFOLIO_DIR / INDEX_FILENAME
        if shared.exists():
            return shared
    except Exception:
        pass  # unreachable shared folder just means "use the local path"
    return INDEX_PATH


def publish_index(index: list[dict] = None) -> Path:
    """
    Copies this machine's local index into the shared team folder, so a
    teammate who has never built one gets the team's real, current index
    instead of an empty comparison on every screen. Local always wins on
    READ (see index_path()) -- this is the separate, explicit WRITE step that
    makes a fresh copy available to whoever has none yet.
    """
    from config import SMARTSHEET_PORTFOLIO_DIR
    data = index if index is not None else load_index(INDEX_PATH)
    dest = SMARTSHEET_PORTFOLIO_DIR / INDEX_FILENAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(dest)
    return dest


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

    Resolves local-then-shared via index_path() unless a specific path is
    given (callers that pass INDEX_PATH explicitly, e.g. a rebuild script,
    still target the local file only).
    """
    p = path or index_path()
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


# Language that means the land is ALREADY platted / already lots. Whole words,
# because "lot" appears inside "lots of frontage" and worse.
_FINISHED_LOT_PATTERN = re.compile(
    r"\b(finished lots?|platted lots?|recorded (?:final )?plat|final plat recorded|"
    r"fully platted|paper lots?|improved lots?|entitled lots?|lot package|"
    r"\d+\s+(?:finished|platted|recorded|improved|entitled)\s+lots?)\b",
    re.I,
)


def looks_like_finished_lots(*texts) -> bool:
    """
    Does this listing describe land that is ALREADY subdivided into lots?

    This is NOT a guess at what the firm would do with it -- that stays out of
    scope for an unowned listing, for the reason compare_listing_row explains.
    It is an observation about the state of the asset: land that is already
    platted cannot be "entitled" again, so the firm's own finished-lot
    acquisitions are the relevant precedent rather than its entitlement plays,
    and those are the most profitable pattern the portfolio actually documents.

    Deliberately narrow. It only fires on explicit language, never on a bare
    "lot", and a listing with nothing to say returns False -- which on a
    typical CoStar export is EVERY row. Measured 2026-08-11: the real 216-row
    export carries no platting language in any column at all, so this is
    dormant there and correctly changes nothing.
    """
    for t in texts:
        s = str(t or "").strip()
        if s and s.lower() != "nan" and _FINISHED_LOT_PATTERN.search(s):
            return True
    return False


def compare_listing_row(state, county, land_type_text, acres, top_n: int = 3,
                         index: list[dict] = None, extra_text=()) -> dict:
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
    # The ONE exception to "never pass a plan_type for a listing", added
    # 2026-08-11. Normally the firm hasn't decided an approach for something it
    # doesn't own, so asserting one would misrepresent an unmade decision. But
    # land that is ALREADY platted is a fact about the asset, not a decision
    # about it -- and without this the firm's eight finished-lot acquisitions,
    # its best-documented profitable pattern, can never surface as precedent
    # for the one kind of listing they actually apply to.
    if looks_like_finished_lots(land_type_text, *extra_text):
        facts["plan_type"] = "acquire-finished-lots"
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

# "still held" covers three completely different situations -- nobody ever
# tried to sell, it was marketed for years and nobody bought, or the investors
# already got their capital back without a sale. As one label it teaches a
# reader nothing, so where a property's own record evidences which, say it.
# Deliberately only populated where evidenced: 10 of 38 as of 2026-08-11, and
# the other 28 stay plain "still held" rather than being assigned a story.
_DISPOSITION_PHRASE = {
    "never-marketed":   "still held, never marketed",
    "marketed-unsold":  "still held, marketed without a buyer",
    "capital-returned": "still held, but capital already returned",
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
    outcome = (_DISPOSITION_PHRASE.get((m.get("disposition_detail") or "").strip().lower())
               or _OUTCOME_PHRASE.get((m.get("outcome_status") or "").strip().lower()))

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

    # Say how much to trust the approach, every time. "[verified]" and
    # "[unconfirmed]" are the two ends worth flagging; a plain summary-derived
    # classification is the unremarkable middle and gets no tag, so the marks
    # stay meaningful instead of decorating every line.
    source = (m.get("plan_type_source") or "").strip().lower()
    if verified or source == "documents":
        head += " [verified]"
    elif source == "unrecorded" and plan:
        head += " [unconfirmed]"
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
            # How the plan_type was arrived at: "documents" (independently
            # re-read from source), "summary" (taken from the property's own
            # written summary), or "unrecorded" (never written down). Measured
            # 2026-08-10: an unrecorded classification was wrong 2 times in 3,
            # against 1 in 8 for a cited one -- so this travels with the match
            # and callers present an unrecorded one as provisional.
            "plan_type_source": record.get("plan_type_source", "unrecorded"),
            "outcome_status": record.get("outcome_status", "unclear"),
            # Which kind of "still held" this is, where the record evidences
            # it. Absent for most properties, and absent means unknown -- not
            # "nothing happened".
            "disposition_detail": record.get("disposition_detail"),
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
