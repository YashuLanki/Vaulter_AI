"""
analysis/screening/fit_screen.py
--------------------------------
Screen a CoStar export by FIT AGAINST THE EXISTING PORTFOLIO, not against
absolute thresholds.

Why this exists (and why it isn't phase1_rules + phase2_ranking)
----------------------------------------------------------------
The 4-phase pipeline predates `docs/COMPANY_PROFILE.md`. Once the profile was
derived from the firm's own deal history, three of Phase 1's hard rules turned
out to contradict it directly. Measured on a real 216-row Arizona export:

  * Phase 1 eliminated 69 listings. **60 of those 69** died on grounds §5 of the
    profile explicitly lists as NOT dealbreakers -- 46 for flood risk, 14 for
    having a structure on site. One acquired parcel had roughly 12% of its
    acreage in the 100-year floodplain and was still bought. Another had a
    golf course, homes and a cell tower on site, and the firm still offered --
    treating the income as a positive.
  * 11 of those eliminations sat within 3 miles of a property the firm already
    owns, including one 87.5-acre residential parcel under 2 miles from an
    existing holding.
  * Long days-on-market was scored as a risk. A senior partner's own stated #1
    rationale on the largest deal in the record was the opposite: a distressed
    basis, bank REO at ~74% below the prior owner's basis.

So this module **eliminates nothing.** Everything is ranked and explained.
§8.1 of the profile is the reason: the firm's rejection history is thin -- 41
foreclosures and 16 cancelled pursuits give real *failure* evidence, but there
is almost no record of deals screened and declined. Hard-eliminating against a
standard nobody has ratified is how you destroy deal flow with no error message.

Market-agnostic by construction
-------------------------------
Nothing in the RANKING is Arizona-specific. Every market-relative number (price
per acre, peer comparisons) is computed from a peer group found inside the
export itself, falling back Submarket Cluster -> Submarket -> County -> Market
-> whole file until one has enough rows. Feed it a Texas, Colorado or Utah
export and it recalibrates on its own. Proximity uses whatever holdings are
geocoded; a market where the firm owns nothing simply reads as "new market",
which per §6 shifts the weight onto size-context rather than disqualifying
anything.

The COSTS are a different matter, and the distinction is load-bearing
------------------------------------------------------------------------
Rewritten 2026-07-28 after a document review of the firm's own budgets,
settlement statements and schedules. See `docs/PORTFOLIO_STANDARD.md` for the
full record and every source path.

What changed and why:

  * `cost_load` -- an invented 0.35 of purchase price -- is gone. Entitlement is
    priced PER LOT in every budget the firm has ever produced, and falls
    meaningfully with project size. A percentage of purchase price was the
    wrong SHAPE, not just the wrong value.
  * `lots_per_acre` fell substantially from its old value. Nothing in the
    record supported the old one; the two most recent deals are both lower.
  * Carry is now charged, at a measured property-tax rate, over the OBSERVED
    hold rather than the underwritten one. It is a floor -- insurance,
    management and maintenance have no per-acre figure on record.
  * Horizontal development stays OUT of the arithmetic, deliberately. It is
    measured, real, and per-acre, but only in Pinal County, and the firm sells
    entitled rather than improved land, so it applies only when the exit comp
    is improved. It is quoted as context on wide-headroom rows instead.

The rule this follows: **a cost with no record is left out and declared, never
estimated.** Non-residential rows carry no entitlement figure because none
exists in the corpus, so `Cost_Basis` says on every one of them that the
required exit is understated. Ranking within a type is unaffected because the
treatment is uniform.

And because that evidence is overwhelmingly Arizona, every run reports
`evidence_coverage` per state: what the portfolio can and cannot say about the
markets in this file. A Texas export ranks normally and says plainly that there
is no Texas cost, timing, exit-price or rejection history to read it against.
Marking an unfamiliar market down would rank the firm's own data coverage
instead of the deals -- the exact bug the neutral proximity floor exists to
prevent.
"""

import logging
import math
import re
from pathlib import Path

import pandas as pd

from config import SCREENING_OUTPUT_DIR

log = logging.getLogger("vaulter.fit_screen")


def _load_cost_assumptions() -> dict:
    """
    Real $/lot and $/acre figures live in system/data/cost_assumptions.json,
    gitignored -- this repo is public, and those are the firm's real numbers.
    Never raises: a missing file is a normal, expected state (a fresh clone of
    the public repo with no local firm data), not an error. Every ASSUMPTIONS
    key sourced from here degrades to None when the file is absent, and every
    use site below treats a None the same as "no record" -- declared, never
    invented. See docs/PORTFOLIO_STANDARD.md (also gitignored) for how each
    real figure was measured.
    """
    import json
    path = Path(__file__).resolve().parents[2] / "data" / "cost_assumptions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


_COST = _load_cost_assumptions()


# ─── Assumptions ──────────────────────────────────────────────────────────────
# EVERY number here is an assumption, not a ratified rule. They are collected in
# one place precisely so a partner can argue with them. See §8 of
# docs/COMPANY_PROFILE.md for what is and isn't established.
#
# Real $/lot and $/acre figures (entitlement anchors, horizontal development,
# the large-ask and exit-path reference figures) are NOT hardcoded here -- this
# file is public, those are the firm's real numbers, and they live in the
# gitignored system/data/cost_assumptions.json instead, loaded above. The
# actual measured values and their sourcing are identical to before; only
# where they're stored changed. Absent that file, every one of them is None,
# and every use site below reports the cost as "no record" rather than
# guessing a substitute.

ASSUMPTIONS = {
    # Vaulter is an opportunistic / value-add predevelopment land investor. It
    # does not underwrite to user or spec-developer comps -- it buys raw or
    # distressed land, does the entitlement work, and sells the entitled
    # position to users and developers expecting this multiple on invested
    # capital. Stated by the firm, 2026-07-28.
    #
    # No firm document states a required multiple as policy. What IS written
    # down, on three independently dated 2025-26 deals, is a 9% preferred
    # return with a 65/35 LP/GP split above it. Base-case model outputs
    # cluster 2.2x-3.4x unlevered. So this target is consistent with the
    # record but is a modelled outcome, not a stated rule.
    "moic_target_low":  2.5,
    "moic_target_high": 3.0,

    # The firm's own average asset value, used only as a reference point on an
    # unusually large ask (see add_cautions) and in the report's headline
    # figures. Real figure lives in the gitignored cost_assumptions.json.
    "avg_asset_value_millions": _COST.get("avg_asset_value_millions"),

    # Residential lot yield, lots per acre. MEASURED across five deals, and the
    # single largest correction in this file: the previous value of 8.0 was
    # roughly double anything in the record. One deal was excluded as an
    # outlier (estate lots, a much lower density than the rest). Of the
    # remaining four, the two most recent deals are the two lowest.
    # COMPANY_PROFILE.md §7's "7-9" is also stale.
    # Range is reported alongside every figure derived from it.
    "lots_per_acre":       3.5,
    "lots_per_acre_low":   2.5,
    "lots_per_acre_high":  4.2,

    # Entitlement soft cost -- engineering, studies, city submittal fees, legal.
    # MEASURED from the firm's own budget workbooks -- see
    # docs/PORTFOLIO_STANDARD.md (gitignored) for the full record and every
    # source path. This is priced PER LOT and falls with project size, which
    # is why the previous "% of purchase price" was the wrong SHAPE, not
    # merely the wrong value: entitlement cost tracks lots created and plan
    # sheets a jurisdiction demands, not what the land cost. Anchors below are
    # interpolated by lot count; see _entitlement_per_lot.
    #
    # Three real Arizona projects, one an invoiced actual, at three different
    # scales -- per-lot cost falls meaningfully as lot count rises. A
    # California project at a different scope (TTM stage only) was excluded.
    # Where actuals exist they came in UNDER budget. The real anchor values
    # themselves live in the gitignored system/data/cost_assumptions.json.
    "entitlement_per_lot_anchors": _COST.get("entitlement_per_lot_anchors"),

    # Annual carry as a fraction of purchase price. MEASURED, but PARTIAL --
    # this is property tax only, from a real observed property-tax history,
    # grown over several years. Another property ran far less off a very low
    # basis, so this rate is the high end of two observations.
    #
    # NOT included, because no figure is on record: insurance, management, site
    # maintenance and any interest. One closing memo PROJECTED management and
    # maintenance costs, but those are pro forma, not actuals, and do not scale
    # per acre. Carry here is therefore a FLOOR.
    "carry_rate_annual": 0.0178,

    # Horizontal development -- streets, sewer, water, grading, walls. MEASURED
    # across four engineer's estimates on two parcels, ALL PINAL COUNTY.
    #
    # DELIBERATELY NOT IN THE ARITHMETIC, for two reasons:
    #   1. The firm sells entitled, not improved, land -- the buyer pays this.
    #      It matters only when the exit comp is improved land, and nothing in a
    #      CoStar export says whether a comp is improved.
    #   2. The evidence is one county. Applying a Pinal figure to a Texas or
    #      Colorado listing would be inventing, which is the failure this whole
    #      rework exists to remove.
    # It is REPORTED as context on wide-headroom rows instead. See add_pricing.
    #
    # These costs are rising fast and the low end is stale: one project's water
    # estimate roughly doubled over about two years on identical design, with
    # the newer unit prices taken from actual homebuilder bids. The other
    # project rose meaningfully over a much shorter window. Real figures live
    # in the gitignored system/data/cost_assumptions.json.
    "horizontal_per_acre_low":   _COST.get("horizontal_per_acre_low"),
    "horizontal_per_acre_high":  _COST.get("horizontal_per_acre_high"),
    "horizontal_evidence_scope": _COST.get("horizontal_evidence_scope", "no local cost data"),

    # Hold period. Underwritten: 30-60 months across five models (30, 36, 54
    # and 60 months across four of them). Actual: 5.9-15.1 years across six
    # completed round-trips. And 21 properties bought 2011-2015 are STILL HELD
    # at 11-15 years, so the completed-deal sample is survivorship-biased
    # toward the ones that could exit.
    #
    # The gap is explained, not mysterious: entitlement schedules slip 2.5-4x.
    # One project went from a 9.6-month plan (Nov 2024) to 23.5 months
    # (May 2026) with the start date unmoved; between the Mar and May 2026
    # revisions, seven weeks elapsed and the finish moved seven weeks. Another
    # project slipped from a 15.2-month plan to 3+ years over.
    "hold_years_underwritten": 4,
    "hold_years_actual":       14,
    "hold_years_actual_low":   6,
    "hold_years_actual_high":  15,
    "schedule_slip_multiple":  (2.5, 4.0),

    # A required exit above this multiple of the peer group's MEDIAN asking
    # price per acre gets flagged. Not a hard limit -- entitled land genuinely
    # does trade well above raw land, which is the entire business model. It
    # flags when the required leap is large enough to deserve an argument.
    "exit_leap_flag": 3.0,

    # Peer group needs at least this many rows to be worth comparing against,
    # otherwise fall back to a broader geography.
    "min_peer_rows": 8,
}

# What finished lots have actually fetched from homebuilders. MEASURED from
# settlement statements; reported as context, never scored, and Arizona-only.
#
# The PRICES live in the gitignored cost file, not here. An earlier pass
# genericized the buyer names but left the real per-lot figures as literals in
# this public file -- caught 2026-08-11. A real sale price is firm-confidential
# whether or not the buyer is named beside it, so it now follows the same rule
# as every other real figure in this module: loaded from
# system/data/cost_assumptions.json, and absent that file the screen simply
# reports it has no record rather than inventing a substitute.
EXIT_LOT_COMPS = tuple(
    (c.get("buyer", "unknown"), c.get("date", ""), c.get("price_per_lot"))
    for c in (_COST.get("exit_lot_comps") or ())
    if c.get("price_per_lot") is not None
)

# Weights for the composite fit score. Proximity dominates because §7 calls it
# "the strongest revealed preference, and mechanically checkable" -- ~34 of 57
# holdings sit in one of 15 clusters.
#
# THESE ARE THE ONLY NUMBERS IN THIS FILE WITH NO EVIDENCE BEHIND THEM. Two
# independent document searches (2026-07-28) found nothing in the corpus that
# ranks or weights selection factors. The closest artifact is a senior
# partner's unordered list on the firm's largest recent acquisition --
# distressed basis, vested entitlements, low off-site cost, prepaid utility
# credits. No document contradicts these weights either. They need a human
# decision; see PORTFOLIO_STANDARD.md §9.
WEIGHTS = {
    "proximity": 35,
    "pricing":   30,
    "distress":  20,
    "size_fit":  15,
}


def _entitlement_per_lot(lots: float) -> float:
    """
    Entitlement soft cost per lot, interpolated by project size from the three
    measured Arizona anchors. Larger projects spread fixed costs over more lots.

    Flat outside the measured range rather than extrapolated -- a very large
    project doesn't get an implausibly tiny per-lot figure, and a very small
    one doesn't get an implausibly large one.
    """
    anchors = ASSUMPTIONS["entitlement_per_lot_anchors"]
    if anchors is None:
        return float("nan")  # no local cost data -- declared as "no record" downstream, never invented
    if not (lots and lots == lots and lots > 0):
        return float("nan")
    if lots <= anchors[0][0]:
        return float(anchors[0][1])
    if lots >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= lots <= x1:
            return float(y0 + (y1 - y0) * (lots - x0) / (x1 - x0))
    return float("nan")


# ─── Column access ────────────────────────────────────────────────────────────
# CoStar exports vary between markets and report vintages. Never index a column
# directly -- a missing column must degrade to "unknown", never raise.

def _col(df: pd.DataFrame, *names, default=None):
    """First matching column as a Series, or a Series of `default`."""
    for n in names:
        if n in df.columns:
            return df[n]
    return pd.Series([default] * len(df), index=df.index)


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce")


# ─── Column resolution: no two CoStar exports have the same columns ───────────
# Established 2026-07-28: a Tucson export arrived with 24 columns and none of
# the names the screener read. It is not one template with optional extras --
# every export is shaped by whoever built the report, and broker spreadsheets
# vary more still. So nothing indexes a raw column name any more. Each concept
# is resolved from a list of candidates, then derived if no candidate matched,
# and the resolution is REPORTED so a reader can see that "Land Area" came from
# a square-footage column or was pulled out of a listing title.
#
# Order matters. `Proposed Land Use` is preferred over `Property Type` because
# a predevelopment investor cares what the land can BECOME, and CoStar's
# Property Type on a land export is the constant "Land" -- present, useless,
# and it would mask a better column if it won.
_FIELD_ALIASES = {
    "Land Area (AC)":  ["Land Area (AC)", "Land Area (Acres)", "Land Area AC",
                        "Acres", "Acreage", "Lot Size (AC)", "Lot Size Acres",
                        "Total Land Area (AC)", "Size (Acres)", "Land (AC)"],
    "For Sale Price":  ["For Sale Price", "Asking Price", "Sale Price",
                        "List Price", "Listing Price", "Price"],
    # "Property Type" is deliberately NOT here. On a land export it is the
    # constant "Land" -- as an alias it would win outright and mask a real
    # land-use column. Dynamic matching can still reach it, where the
    # prefer-a-column-that-varies rule keeps it in its place.
    "Secondary Type":  ["Secondary Type", "Proposed Land Use", "Property Subtype",
                        "Sub Type", "Land Use"],
    "Latitude":        ["Latitude", "Lat"],
    "Longitude":       ["Longitude", "Long", "Lng", "Lon"],
    "Days On Market":  ["Days On Market", "Days on Market", "DOM", "Days Listed"],
    "Last Sale Price": ["Last Sale Price", "Prior Sale Price", "Previous Sale Price"],
    "Market Name":     ["Market Name", "Market"],
    "Submarket Name":  ["Submarket Name", "Submarket"],
    "County Name":     ["County Name", "County"],
    "Property Address": ["Property Address", "Address", "Street Address"],
    "City":            ["City", "City Name"],
    "State":           ["State", "State Name", "St"],
}

# Square feet per acre, for the commonest derivation.
_SQFT_PER_ACRE = 43560.0
_ACRES_IN_SF = ["Land Area (SF)", "Lot Size (SF)", "Land Area SF", "Land SF",
                "Square Feet", "Total Land Area (SF)"]

# "RARE! 11.77 acres NW corner I-10", "±73.55 acres at NWC Moore Rd", "156 AC in
# Southwest Tucson" -- brokers put the size in the title when there is no size
# column. Recovers a minority of rows, which beats none, and is labelled as
# derived so nobody mistakes it for a CoStar field.
_ACRES_IN_TEXT = re.compile(r"(\d[\d,]*\.?\d*)\s*(?:\+/-\s*)?(?:ac\b|acres?\b)", re.I)


# Dynamic matching, for the names nobody anticipated. The alias lists above are
# a fast path; this is the fallback that catches "Gross Site Area", "Total
# Acres", "Site Size", "Ask Price" and whatever else a broker types.
#
# Two-part test, and BOTH parts are needed. Name patterns alone match the wrong
# column readily -- "Floodplain Area" would win the acreage slot on the word
# "area", and "For Sale Price Per SF" would win the price slot. So a candidate
# must look right by name AND hold values of the right kind and magnitude.
_CONCEPT_RULES = {
    "Land Area (AC)": {
        "strong": [r"\bacres?\b", r"\bac\b", r"\bacreage\b"],
        "weak":   [r"(land|lot|site|parcel).*(area|size)", r"(area|size).*(land|lot|site)"],
        # Square footage must never land here. "Land Area (SF)" matches the weak
        # land+area pattern, and 1.7 million square feet passed a generous range
        # check, so a 40-acre parcel was read as 1.7 million acres -- silently,
        # with every downstream figure wrong. It belongs in the SF->AC
        # derivation below, not in this slot.
        "avoid":  [r"\bper\b", r"price", r"flood", r"zone", r"building", r"rentable",
                   r"\bfar\b", r"coverage", r"\bsf\b", r"square\s*f", r"sq\s*ft"],
        # A median above this is not a parcel size in acres -- the firm's
        # largest evaluated deal was 4,508 acres, and this is four times that.
        # Anything bigger is a different unit wearing the wrong name, and
        # falls through to the conversion path.
        #
        # The ceiling was 100,000 and that was too generous to do its job. It
        # only ever caught square footage on BIG parcels: a "Lot Size" column
        # (no SF marker in the name, so the `avoid` list cannot see it) holding
        # square feet for half-acre pads has a median around 29,000 -- which
        # passed, and every one of those parcels was read as 29,000 acres.
        # Measured: `Lot Size` with a median of 29,120 sq ft won the acreage
        # slot outright. At 20,000 that column is rejected and the rows abstain,
        # which is the correct outcome when the unit cannot be established.
        "numeric": True, "lo": 0.005, "hi": 20_000,
    },
    "For Sale Price": {
        "strong": [r"(for\s*sale|asking|list(ing)?|sale|purchase)\s*price", r"^price$",
                   r"\bask\b"],
        "weak":   [r"price"],
        # The unit tokens are matched as WORDS, not as "/sf". `_norm` turns all
        # punctuation into spaces before these run, so the old `/\s*(sf|ac|unit|
        # room)` could never match anything -- the slash was already gone.
        # Measured consequence: a `Price/Acre` column normalises to "price acre",
        # dodged the avoid list, and won the asking-price slot -- a per-acre
        # figure used as the total purchase price, with every downstream
        # number wrong and nothing to show for it.
        "avoid":  [r"\bper\b", r"\b(sf|ac|acre|acres|unit|units|room|rooms)\b",
                   r"last|prior|previous|sold", r"rent", r"assessed|tax"],
        "numeric": True, "lo": 1000, "hi": 5_000_000_000,
    },
    "Secondary Type": {
        "strong": [r"proposed\s*land\s*use", r"secondary\s*type", r"sub\s*-?\s*type",
                   r"land\s*use"],
        "weak":   [r"\btype\b", r"category", r"\buse\b"],
        "avoid":  [r"energy|leed|loan|sale\s*type|owner|contact|collateral|construction"],
        "numeric": False,
    },
    # The coordinate rules lean on the range check rather than the name: a GIS
    # export calls these "Y Coord"/"X Coord", and a UTM column of the same name
    # holds values in the hundreds of thousands, which the range rejects.
    "Latitude":  {"strong": [r"^lat(itude)?$"], "weak": [r"lat", r"y.?coord", r"^y$"],
                  "avoid": [r"long"], "numeric": True, "lo": -90, "hi": 90},
    "Longitude": {"strong": [r"^lon(g(itude)?)?$", r"^lng$"],
                  "weak": [r"long|lng", r"x.?coord", r"^x$"],
                  "avoid": [], "numeric": True, "lo": -180, "hi": 180},
    "Days On Market": {
        "strong": [r"days\s*on\s*market", r"^dom$", r"days\s*listed"],
        "weak":   [r"\bdays\b"], "avoid": [r"price|sale\s*date"],
        "numeric": True, "lo": 0, "hi": 40_000,
    },
    "Last Sale Price": {
        "strong": [r"(last|prior|previous)\s*sale\s*price"],
        "weak":   [r"(last|prior|previous).*price"],
        # Same normalisation trap as For Sale Price above: match unit words.
        "avoid":  [r"\bper\b", r"\b(sf|ac|acre|acres|unit|units)\b"],
        "numeric": True, "lo": 1, "hi": 5_000_000_000,
    },
    "Market Name":    {"strong": [r"^market(\s*name)?$"], "weak": [r"market"],
                       "avoid": [r"sub", r"days|rent|cap"], "numeric": False},
    "Submarket Name": {"strong": [r"^submarket(\s*name)?$"], "weak": [r"submarket"],
                       "avoid": [r"cluster"], "numeric": False},
    "County Name":    {"strong": [r"^county(\s*name)?$"], "weak": [r"county"],
                       "avoid": [], "numeric": False},
    "Property Address": {"strong": [r"^(property\s*)?address$", r"street\s*address"],
                         "weak": [r"address"], "avoid": [r"owner|sale\s*company|mailing"],
                         "numeric": False},
    "City":  {"strong": [r"^city(\s*name)?$"], "weak": [r"\bcity\b"],
              "avoid": [r"owner|company|state\s*zip"], "numeric": False},
    "State": {"strong": [r"^state(\s*name)?$"], "weak": [r"\bstate\b"],
              "avoid": [r"owner|company|city"], "numeric": False},
}


def _norm(name: str) -> str:
    """Column name to a comparable form: lowercase, punctuation to spaces."""
    return re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()


def _plausible(series: pd.Series, rule: dict) -> bool:
    """Do this column's VALUES match what the concept should hold?"""
    vals = series.dropna()
    if vals.empty:
        return False
    if not rule.get("numeric"):
        # Text concepts must not be a numeric column wearing a promising name.
        return _num(vals).notna().mean() < 0.9
    nums = _num(vals).dropna()
    if nums.empty or len(nums) < max(1, 0.05 * len(series)):
        return False
    mid = nums.median()
    return rule["lo"] <= mid <= rule["hi"]


def _match_dynamically(df: pd.DataFrame, canon: str) -> str | None:
    """
    Best column for a concept by name pattern plus value plausibility.

    Strong name matches beat weak ones; among equals, the column with the most
    values wins. Returns None rather than guessing when nothing is plausible --
    a wrong column here is worse than a missing one, because it would look like
    data.
    """
    rule = _CONCEPT_RULES.get(canon)
    if not rule:
        return None
    best, best_key = None, ()
    for col in df.columns:
        n = _norm(col)
        if any(re.search(p, n) for p in rule.get("avoid", [])):
            continue
        rank = (3 if any(re.search(p, n) for p in rule["strong"])
                else 1 if any(re.search(p, n) for p in rule.get("weak", [])) else 0)
        if not rank or not _plausible(df[col], rule):
            continue
        # Among equally good name matches, prefer the column that actually
        # discriminates. On a land export "Property Type" is the constant
        # "Land" -- it fills every row and tells you nothing, and without this
        # it beat a real land-use column purely on column order.
        key = (rank, df[col].nunique(dropna=True) > 1, int(df[col].notna().sum()))
        if key > best_key:
            best, best_key = col, key
    return best


def _first_present(df: pd.DataFrame, names: list[str]) -> str | None:
    """
    Resolve a concept: exact/alias match first, then dynamic matching.

    The alias list is preferred because an exact CoStar name is unambiguous.
    Dynamic matching only runs when no known name is present and populated.
    """
    for n in names:
        if n in df.columns and df[n].notna().any():
            return n
    return _match_dynamically(df, names[0])


def _header_row(path: Path, scan: int = 12) -> int:
    """
    Which row holds the column names.

    Spreadsheets that came via a broker often open with a title, a logo row or
    a blank line, and pandas would take that as the header and find nothing
    afterwards. A header row is: several non-empty cells, nearly all distinct,
    mostly not numbers, and — the deciding signal — at least one cell that reads
    like something the screener knows about.

    Returns 0 when nothing scores better, so an ordinary file is unaffected.
    """
    try:
        if path.suffix.lower() in (".xlsx", ".xls", ".xlsm"):
            raw = pd.read_excel(path, header=None, nrows=scan)
        else:
            # Read the CSV as raw text and split by hand. pandas cannot parse a
            # file whose first line has one field and whose fourth has 290 --
            # it fixes the column count from the first row and then throws.
            import csv as _csv
            with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
                rows = [r for _, r in zip(range(scan), _csv.reader(fh))]
            raw = pd.DataFrame(rows)
    except Exception as e:
        log.warning(f"Could not scan for a header row ({e}); assuming the first.")
        return 0
    if raw.empty:
        return 0

    known = [p for r in _CONCEPT_RULES.values() for p in r["strong"]]
    best, best_score = 0, -1.0
    for i in range(len(raw)):
        cells = [str(v).strip() for v in raw.iloc[i] if pd.notna(v) and str(v).strip()]
        if len(cells) < 3:
            continue
        distinct = len(set(cells)) / len(cells)
        texty = sum(1 for c in cells if _num(pd.Series([c])).isna().all()) / len(cells)
        hits = sum(1 for c in cells if any(re.search(p, _norm(c)) for p in known))
        score = min(len(cells), 30) * 0.05 + distinct * 2 + texty * 2 + hits * 3
        if score > best_score:
            best, best_score = i, score
    if best:
        log.info(f"Header row detected at line {best + 1}, not the first.")
    return best


def normalise_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """
    Fill the column names the screener reads from whatever this export provides.

    Returns the frame plus a provenance report -- one row per concept, saying
    which column it came from, whether it was derived, and how many listings it
    covers. Nothing is invented: a concept with no source is simply absent, and
    the report says so.
    """
    df = df.copy()
    report = []

    for canon, aliases in _FIELD_ALIASES.items():
        src = _first_present(df, aliases)
        if src and src != canon:
            df[canon] = df[src]
        note = ("" if src == canon else f"taken from '{src}'") if src else ""
        report.append({"field": canon, "source": src or "", "derived": "",
                       "note": note,
                       "rows": int(df[canon].notna().sum()) if canon in df.columns else 0})

    def _entry(field):
        return next(r for r in report if r["field"] == field)

    # Acreage is the denominator of almost everything, so it gets two fallbacks.
    acres = _entry("Land Area (AC)")
    if not acres["rows"]:
        sf_col = next((c for c in _ACRES_IN_SF if c in df.columns and df[c].notna().any()),
                      None)
        # Dynamic fallback for a square-footage column named something else.
        if not sf_col:
            for c in df.columns:
                n = _norm(c)
                if re.search(r"(land|lot|site|parcel).*(sf|square|sq ft)", n) or \
                   re.search(r"(sf|square feet|sq ft).*(land|lot|site)", n):
                    if _num(df[c]).notna().any():
                        sf_col = c
                        break
        if sf_col:
            df["Land Area (AC)"] = _num(df[sf_col]) / _SQFT_PER_ACRE
            acres.update(source=sf_col, derived="converted from square feet",
                         note=f"converted from '{sf_col}'",
                         rows=int(df["Land Area (AC)"].notna().sum()))

    if not acres["rows"]:
        text = pd.Series("", index=df.index)
        for c in ("Property Name", "Property Address", "Address"):
            if c in df.columns:
                text = text.str.cat(_text(df[c]), sep=" ")
        pulled = text.str.extract(_ACRES_IN_TEXT, expand=False)
        pulled = _num(pulled.str.replace(",", "", regex=False))
        if pulled.notna().any():
            df["Land Area (AC)"] = pulled
            acres.update(source="listing title/address", derived="parsed from text",
                         note="parsed out of the listing title — only where a size was written in",
                         rows=int(pulled.notna().sum()))

    # A multi-valued land use ("Apartment Units, Apartment Units - Condo, ...")
    # would splinter peer groups, so keep the first use only.
    st = _entry("Secondary Type")
    if st["source"] and st["source"] != "Secondary Type":
        df["Secondary Type"] = _text(df["Secondary Type"]).str.split(",").str[0].str.strip()

    # A missing land type must SAY it is missing. Left blank it rendered as an
    # empty gap in every summary line, which reads as a formatting fault
    # rather than as absent data. "Unknown" is also the key the
    # peer-group tables already use internally, so nothing downstream changes.
    # Real: 9 of 50 rows on the Tucson export have no Proposed Land Use.
    if "Secondary Type" in df.columns:
        df["Secondary Type"] = _text(df["Secondary Type"]).str.strip().replace("", "Unknown")
    else:
        df["Secondary Type"] = "Unknown"

    return df, report


def _text(series: pd.Series) -> pd.Series:
    """Always-a-string view of a column. CoStar leaves blanks as float NaN, so
    a bare .astype(str) still yields the string 'nan' while a missing column
    yields real floats -- both break .lower() downstream."""
    return series.fillna("").astype(str)


def _attach(df: pd.DataFrame, cols: dict) -> pd.DataFrame:
    """Add many columns at once. Assigning them one at a time fragments the
    frame and pandas emits a PerformanceWarning per column."""
    return pd.concat([df, pd.DataFrame(cols, index=df.index)], axis=1)


# ─── Proximity to the existing portfolio ──────────────────────────────────────

def _haversine_miles(lat1, lon1, lat2, lon2) -> float:
    R = 3958.8
    rad = math.radians
    dlat, dlon = rad(lat2 - lat1), rad(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(rad(lat1)) * math.cos(rad(lat2)) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_holdings() -> pd.DataFrame:
    """
    Geocoded portfolio holdings. Empty frame (not an error) if unavailable --
    the screen still runs, it just can't score proximity.
    """
    from config import DATA_DIR
    from pipeline import property_coordinates

    path = property_coordinates.coords_path(DATA_DIR)
    if not Path(path).exists():
        log.warning("No property_coordinates.csv -- proximity scoring disabled.")
        return pd.DataFrame(columns=["property_name", "state", "latitude", "longitude"])

    df = pd.read_csv(path)
    df = df[df["latitude"].notna() & df["longitude"].notna()]
    return df


# Distance bands. §6's finding is that the firm's failures were large parcels in
# markets where it had no adjacent presence, and its recovery in the same market
# was small infill -- so "how close is this to something we already run?" is the
# organising question.
#
# The floor is NEUTRAL (50), not punitive. This matters enormously outside the
# firm's two core states and was a measured bug: with all 44 holdings geocoded
# the Arizona export produced 12 Tier-1 listings; with only 2 holdings it
# produced 2, and Tier 4 grew from 74 to 158 -- on identical listings. Proximity
# was scoring how complete the geocoding happened to be, not how good the deals
# were. The firm has 20 AZ and 19 CA holdings geocoded but only 4 TX and 1 CO,
# so a Dallas export would have been ranked largely by distance-to-Forney.
#
# With a neutral floor, "no holding nearby" adds the same constant to every row
# and therefore cannot reorder anything -- the dimension goes inert instead of
# distorting, and pricing/size/distress decide. Being near a holding still
# lifts a listing, which is the real signal. An absent signal should abstain,
# not vote against.
_TIERS = [
    (3.0,   "Inside cluster",   100),
    (10.0,  "Adjacent",          85),
    (25.0,  "Same area",         70),
    (75.0,  "Known market",      60),
    (1e9,   "New market",        50),
]
_NEUTRAL_PROXIMITY = 50


def add_proximity(df: pd.DataFrame, holdings: pd.DataFrame) -> pd.DataFrame:
    lat_c, lon_c = _num(_col(df, "Latitude")), _num(_col(df, "Longitude"))

    names, dists, tiers, scores = [], [], [], []
    hold = list(holdings.itertuples()) if len(holdings) else []

    for lat, lon in zip(lat_c, lon_c):
        if pd.isna(lat) or pd.isna(lon) or not hold:
            names.append(""); dists.append(float("nan"))
            tiers.append("Unknown"); scores.append(_NEUTRAL_PROXIMITY)
            continue
        best_d, best_n = min(
            (_haversine_miles(lat, lon, h.latitude, h.longitude), h.property_name)
            for h in hold
        )
        tier, score = next((t, s) for lim, t, s in _TIERS if best_d < lim)
        names.append(best_n); dists.append(round(best_d, 2))
        tiers.append(tier); scores.append(score)

    return _attach(df, {
        "Nearest_Holding": names,
        "Distance_Mi": dists,
        "Cluster_Tier": tiers,
        "_proximity_score": scores,
    })


# ─── Size in context (§6) ─────────────────────────────────────────────────────

def add_size_context(df: pd.DataFrame) -> pd.DataFrame:
    """
    §6: "the real standard is not 'small good, large bad'."

      * Small (6-40ac) is normal INSIDE an existing cluster
      * Large (500-700ac) is normal AS a master-plan assemblage
      * Large AND standalone in a market with no presence is THE documented
        failure mode -- 41 CA foreclosures were overwhelmingly 200-640 acre
        desert parcels, and the firm's recovery in that same market was 6-37
        acre infill.
    """
    acres = _num(_col(df, "Land Area (AC)"))
    near = df["Cluster_Tier"]

    verdicts, scores = [], []
    for ac, tier in zip(acres, near):
        inside = tier == "Inside cluster"
        in_cluster = tier in ("Inside cluster", "Adjacent")
        if pd.isna(ac):
            verdicts.append("Unknown acreage"); scores.append(50); continue

        # Same neutral-floor principle as proximity: absence of a nearby holding
        # is often just a geocoding gap, and it is already reflected in the
        # proximity dimension. Penalising it again here would double-count it,
        # which is what dragged whole non-core markets down. Only the ONE
        # combination the corpus actually documents as a failure -- large AND
        # standalone, the 200-640 acre desert parcels behind 41 California
        # foreclosures -- scores below neutral.
        if ac >= 200 and not in_cluster:
            verdicts.append("LARGE + standalone — documented failure mode (§6)")
            scores.append(20)
        elif ac >= 200:
            verdicts.append("Large, but near existing holdings — assemblage pattern")
            scores.append(75)
        elif ac < 20 and inside:
            verdicts.append("Small infill inside a cluster — normal")
            scores.append(95)
        elif ac < 20 and in_cluster:
            verdicts.append("Small, near existing holdings")
            scores.append(80)
        elif ac < 20:
            verdicts.append("Small parcel — needs a cluster or an entitlement angle")
            scores.append(50)
        else:
            verdicts.append("Mid-size (20–200ac) — the portfolio's core band")
            scores.append(80 if in_cluster else 65)
    return _attach(df, {"Size_Context": verdicts, "_size_score": scores})


# ─── Pricing, from a 2.5–3x MOIC perspective ──────────────────────────────────
#
# The value-add mechanism is SUBDIVISION AND ENTITLEMENT: buy one large cheap
# parcel, do the approvals, sell smaller entitled parcels (or finished lots) at
# a much higher price per acre. So the exit comparison must be against a
# SMALLER size band than the purchase, not the same one.
#
# Getting this wrong was a real, measured error. An earlier version compared
# every listing to same-type peers in the same submarket regardless of size. In
# Pinal County, small commercial parcels ask many times more per acre than
# large ones -- a large spread driven purely by parcel size. A 293-acre assemblage
# was therefore scored against 9-acre retail pads and looked like a bargain when
# it wasn't. Land price per acre falls steeply with parcel size in every market;
# any per-acre comparison that ignores size is meaningless.

# Absolute acre bands, not quantiles. These map to real development products
# rather than to the shape of one export: a pad or single subdivision, a
# neighbourhood-scale parcel, a master-planned assemblage. An acre is an acre in
# Texas or Colorado, so these travel; the PRICES attached to them are always
# derived locally.
_BANDS = [(20, "<20ac"), (100, "20-100ac"), (float("inf"), "100ac+")]

# What a parcel of each band realistically exits AS, after subdivision.
_EXIT_BAND = {"100ac+": "20-100ac", "20-100ac": "<20ac", "<20ac": "<20ac"}

# Some CoStar "Secondary Type" values describe the CURRENT use, not a product
# anyone exits into. Nobody entitles farmland in order to sell smaller farmland
# -- the exit is residential or commercial. Left unmapped, agricultural parcels
# found no exit comp at all (n=0) and fell out of the pricing test entirely.
# The cheaper of the candidate exit types is used, so this never flatters.
_EXIT_TYPE_CANDIDATES = {
    "Agricultural": ["Residential", "Commercial"],
    "Pasture/Ranch": ["Residential", "Commercial"],
    "Timberland": ["Residential", "Commercial"],
    "Open Space": ["Residential", "Commercial"],
}


# What counts as a RESIDENTIAL product, for the entitlement cost and for the
# exit-path note. Every measured per-lot entitlement budget the firm has
# produced is a residential subdivision, so this is the set those figures
# legitimately apply to.
#
# Matching the literal word "Residential" was not enough, and the miss was
# silent. CoStar's `Proposed Land Use` names the same product a dozen ways --
# "Single Family Development", "Single Family Residence", "Apartment Units",
# "MultiFamily" -- and none contain it. Measured on the 50-row Tucson export:
# 0 of 50 rows carried an entitlement cost, and every residential one was
# labelled "no entitlement cost on record for non-residential", which is a
# false statement about the firm's own budgets rather than a missing number.
_RESIDENTIAL_PAT = (r"residential|single[\s-]*family|multi[\s-]*family|apartment|"
                    r"condo|townhom|townhous|duplex|subdivision|"
                    r"manufactured\s*hous|mobile\s*home")


def _size_band(acres) -> str:
    if pd.isna(acres):
        return "unknown"
    return next(label for limit, label in _BANDS if acres < limit)


def _as_num(v) -> float:
    """A cell as a float, or NaN. Blank cells arrive as None, '' or NaN."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _untestable_because(price, acres, n) -> str:
    """
    Why Exit_Headroom is undefined for this row -- in the reader's words.

    Four distinct causes were previously collapsed into one sentence, "No price
    or no comparable exit product". Next to a visible asking price that reads as
    a bug, and it hid the commonest cause on a thin export: no parcel size, so
    there is no $/acre to require anything of. Each cause has a different fix
    (re-export with the column, or accept the file cannot answer it), so each
    gets its own sentence.
    """
    has_price = pd.notna(_as_num(price))
    a = _as_num(acres)
    has_size = pd.notna(a) and a > 0
    if not has_price and not has_size:
        return "neither an asking price nor a parcel size in this export"
    if not has_price:
        return "no asking price in this export"
    if not has_size:
        return "no parcel size in this export, so there is no $/acre to test"
    return "no comparable exit product in this file to measure it against"


def _band_price_table(df: pd.DataFrame, ppa: pd.Series) -> dict:
    """
    Median asking $/acre for every (geography, type, size-band) cell, at each
    geography level, plus the row count behind it.

    Returns {level: {(geo, type, band): (median, n)}} so the caller can walk
    outward from the tightest geography to the loosest until a cell has enough
    rows. Everything is derived from the export itself -- feed it Texas and it
    calibrates to Texas.
    """
    kind = _text(_col(df, "Secondary Type", default="Unknown")).replace("", "Unknown")
    band = _num(_col(df, "Land Area (AC)")).map(_size_band)

    levels = {}
    for name, cols in (
        ("cluster",   ["Submarket Cluster"]),
        ("submarket", ["Submarket Name"]),
        ("county",    ["County Name"]),
        ("market",    ["Market Name"]),
    ):
        if not all(c in df.columns for c in cols):
            continue
        # fillna("") before astype(str), not after: pandas 3.0's string dtype
        # stopped stringifying NaN to the literal text "nan" on .astype(str)
        # (it stays a real missing value instead), so a row with any blank
        # geography column crashed .agg(" / ".join, ...) with "expected str
        # instance, float found" -- found 2026-08-11 testing a fresh install's
        # unpinned pandas, which resolved to 3.0.5.
        geo = df[cols].fillna("").astype(str).agg(" / ".join, axis=1)
        table = {}
        for key, idx in pd.DataFrame({"g": geo, "k": kind, "b": band}).groupby(
                ["g", "k", "b"]).groups.items():
            vals = ppa.loc[idx].dropna()
            if len(vals):
                table[key] = (float(vals.median()), len(vals))
        levels[name] = table

    # Loosest fallback: type + band across the whole file, ignoring geography.
    table = {}
    for key, idx in pd.DataFrame({"k": kind, "b": band}).groupby(["k", "b"]).groups.items():
        vals = ppa.loc[idx].dropna()
        if len(vals):
            table[key] = (float(vals.median()), len(vals))
    levels["file"] = table
    return levels


def _lookup_one(levels: dict, geos: dict, kind: str, exit_band: str, minimum: int):
    """Best comp for one exit type: tightest geography with enough rows."""
    for level in ("cluster", "submarket", "county", "market"):
        table, geo = levels.get(level), geos.get(level)
        if not table or geo is None:
            continue
        hit = table.get((geo, kind, exit_band))
        if hit and hit[1] >= minimum:
            return hit[0], hit[1], f"{geo} · {kind} · {exit_band}"
    hit = levels["file"].get((kind, exit_band))
    if hit and hit[1] >= max(3, minimum // 3):
        return hit[0], hit[1], f"all markets · {kind} · {exit_band}"
    for level in ("cluster", "submarket", "county", "market"):
        table, geo = levels.get(level), geos.get(level)
        if table and geo is not None:
            hit = table.get((geo, kind, exit_band))
            if hit:
                return hit[0], hit[1], f"{geo} · {kind} · {exit_band} (thin)"
    return None


def _lookup_exit_comp(levels: dict, geos: dict, kind: str, exit_band: str, minimum: int):
    """
    Median asking $/acre for the product this parcel would exit as.

    For a current-use label like Agricultural, the exit is a different type
    entirely (see _EXIT_TYPE_CANDIDATES) -- the cheapest available candidate is
    taken so the test is never flattered by picking the richest exit.

    Returns (median_price_per_acre, n, description).
    """
    candidates = _EXIT_TYPE_CANDIDATES.get(kind, [kind])
    hits = [h for h in (_lookup_one(levels, geos, c, exit_band, minimum) for c in candidates) if h]
    if not hits:
        return float("nan"), 0, "no comparable exit parcels in this file"
    best = min(hits, key=lambda h: h[0])
    if len(candidates) > 1:
        return best[0], best[1], f"{best[2]} (exit product for {kind})"
    return best


def add_pricing(df: pd.DataFrame, moic: float) -> pd.DataFrame:
    """
    Vaulter is not a user or a spec developer, so the question is never "is this
    priced fairly against comps." It is:

        At this ask, what must the entitled position sell for to return `moic`
        on invested capital -- and can that be achieved by subdividing and
        entitling into the parcel sizes this market actually pays up for?

    Two paths, because the profile (§4) is explicit that they differ:

      * RESIDENTIAL exits per LOT. Reported as an implied $/lot at the target
        multiple, cross-checked against the firm's own 2023 models.
      * EVERYTHING ELSE exits per acre of smaller entitled parcel.

    Both are scored on the same measure -- Exit_Headroom, the ratio of what the
    market pays for the exit product to what this deal needs it to pay.
    Above 1.0 clears the target; below 1.0 does not, at this ask.
    """
    acres = _num(_col(df, "Land Area (AC)"))
    price = _num(_col(df, "For Sale Price"))
    kind_s = _text(_col(df, "Secondary Type", default="Unknown")).replace("", "Unknown")

    ask_per_acre = price / acres.replace(0, pd.NA)

    # Invested per acre = purchase + entitlement + carry.
    #
    # Entitlement is MEASURED per lot (see ASSUMPTIONS) and interpolated by
    # project size, so it needs a lot count -- which only exists for
    # residential. For every other type the corpus holds no entitlement figure
    # at all. Rather than invent one, it is left out and the row says so:
    # Cost_Basis carries the caveat on every non-residential row. That
    # understates their required exit, which is stated rather than hidden;
    # ranking WITHIN a type is unaffected because the treatment is uniform.
    is_resi = kind_s.str.contains(_RESIDENTIAL_PAT, case=False, regex=True, na=False)
    lots_per_ac = ASSUMPTIONS["lots_per_acre"]
    # Floored at one lot on any parcel that has a size. Without the floor a
    # parcel under 0.29 acres rounded to ZERO lots, _entitlement_per_lot(0)
    # returned NaN, and the NaN propagated through invested_per_acre all the way
    # to Exit_Headroom -- so a small residential listing WITH a price and WITH a
    # comp came back "return multiple untestable". Measured on a 0.10-acre row.
    indicative_lots = (acres * lots_per_ac).round(0).clip(lower=1).where(acres > 0)
    entitlement_per_acre = (indicative_lots.map(_entitlement_per_lot)
                            * lots_per_ac).where(is_resi, 0.0)

    # Property tax only, over the ACTUAL observed hold rather than the
    # underwritten one -- deals in the record ran 6-15 years, not 4. A floor:
    # insurance, management and maintenance have no per-acre figure on record.
    carry_per_acre = (ask_per_acre * ASSUMPTIONS["carry_rate_annual"]
                      * ASSUMPTIONS["hold_years_actual"])

    invested_per_acre = ask_per_acre + entitlement_per_acre + carry_per_acre
    required_exit = invested_per_acre * moic

    # A missing local cost file (see ASSUMPTIONS) means no entitlement figure
    # exists for ANY row, not just non-residential -- same "declared, never
    # invented" treatment, just applied uniformly instead of by land type.
    have_entitlement_data = ASSUMPTIONS["entitlement_per_lot_anchors"] is not None
    cost_basis = pd.Series(
        ["purchase + entitlement + property-tax carry" if (r and have_entitlement_data) else
         "purchase + property-tax carry ONLY — no entitlement cost on record for "
         "non-residential, so the required exit below is understated" if have_entitlement_data else
         "purchase + property-tax carry ONLY — no local entitlement cost data available, "
         "so the required exit below is understated"
         for r in is_resi], index=df.index)

    levels = _band_price_table(df, ask_per_acre)
    minimum = ASSUMPTIONS["min_peer_rows"]

    geo_cols = {"cluster": ["Submarket Cluster"], "submarket": ["Submarket Name"],
                "county": ["County Name"], "market": ["Market Name"]}
    geo_series = {
        # fillna("") first -- see _band_price_table's identical fix above for why.
        lvl: df[cols].fillna("").astype(str).agg(" / ".join, axis=1) if all(c in df.columns for c in cols) else None
        for lvl, cols in geo_cols.items()
    }

    bands, exit_bands, comps, ns, bases, headrooms = [], [], [], [], [], []
    for i in df.index:
        band = _size_band(acres.loc[i])
        exit_band = _EXIT_BAND.get(band, band)
        geos = {lvl: (s.loc[i] if s is not None else None) for lvl, s in geo_series.items()}
        comp, n, basis = _lookup_exit_comp(levels, geos, kind_s.loc[i], exit_band, minimum)

        req = required_exit.loc[i]
        headroom = (comp / req) if (pd.notna(req) and req and pd.notna(comp)) else float("nan")

        bands.append(band); exit_bands.append(exit_band)
        comps.append(round(comp) if pd.notna(comp) else float("nan"))
        ns.append(n); bases.append(basis); headrooms.append(round(headroom, 2) if pd.notna(headroom) else float("nan"))

    scores, verdicts, confidence = [], [], []
    for h, n, band, pr, ac in zip(headrooms, ns, bands, price, acres):
        if pd.isna(h):
            # Four distinct causes, not one blurred sentence. See
            # _untestable_because -- printing "no asking price" beside a
            # visible asking price is what made this worth splitting.
            cause = _untestable_because(pr, ac, n)
            scores.append(40)
            verdicts.append(f"{cause[:1].upper()}{cause[1:]} — return multiple untestable")
        elif h >= 4.0:
            # Headroom this wide almost always means the exit comp is improved
            # or finished land while the subject is raw. Horizontal development
            # -- the cost of getting from one to the other -- is deliberately
            # not in this arithmetic (see ASSUMPTIONS), so quote the measured
            # range and its scope instead of celebrating the number.
            lo = ASSUMPTIONS["horizontal_per_acre_low"]
            hi = ASSUMPTIONS["horizontal_per_acre_high"]
            scope = ASSUMPTIONS["horizontal_evidence_scope"]
            cost_note = (f"not costed here and measured ${lo:,}–${hi:,}/acre in {scope}, rising"
                         if lo is not None and hi is not None
                         else "not costed here, and no local cost figure is on record")
            scores.append(75)
            verdicts.append(
                f"Exit market pays {h:.1f}x what this needs — implausibly wide; the exit comp is "
                f"likely improved land while this is raw. Streets, sewer, water and grading are "
                f"{cost_note}"
            )
        elif h >= 2.0:
            scores.append(100); verdicts.append(f"Exit market pays {h:.1f}x what this needs — wide headroom")
        elif h >= 1.3:
            scores.append(85);  verdicts.append(f"Exit market pays {h:.1f}x what this needs — clears")
        elif h >= 1.0:
            scores.append(65);  verdicts.append(f"Exit market pays {h:.1f}x what this needs — tight")
        elif h >= 0.7:
            scores.append(35);  verdicts.append(f"Exit market pays only {h:.1f}x what this needs — short at this ask")
        else:
            scores.append(15);  verdicts.append(f"Exit market pays only {h:.1f}x what this needs — priced like the upside already happened")
        # A parcel already in the smallest band cannot subdivide into anything
        # smaller, so its entire lift has to come from entitlement in place.
        if band == "<20ac" and not pd.isna(h):
            verdicts[-1] += "; no subdivision headroom — lift must come from entitlement alone"
            scores[-1] = max(scores[-1] - 15, 10)
        confidence.append("high" if n >= minimum else "medium" if n >= 3 else "low")

    # Residential sanity check, in the unit the firm actually underwrites --
    # and against what finished lots have really fetched (EXIT_LOT_COMPS).
    implied_per_lot = (pd.Series(comps, index=df.index) / lots_per_ac).round(0)

    cols = {
        "Ask_Per_Acre": ask_per_acre.round(0),
        "Size_Band": bands,
        "Exit_As_Band": exit_bands,
        "Cost_Basis": cost_basis,
        "Entitlement_Per_Acre": entitlement_per_acre.round(0).where(is_resi),
        "Carry_Per_Acre": carry_per_acre.round(0),
        "Required_Exit_Per_Acre": required_exit.round(0),
        "Exit_Comp_Per_Acre": comps,
        "Exit_Headroom": headrooms,
        "Exit_Comp_Basis": bases,
        "Exit_Comp_N": ns,
        "Pricing_Confidence": confidence,
        "Pricing_Verdict": verdicts,
        "_pricing_score": scores,
        "Indicative_Lots": indicative_lots.where(is_resi),
        "Implied_Exit_Per_Lot": implied_per_lot.where(is_resi),
    }
    # Time is what turns a good multiple into a mediocre return. The firm's own
    # published multiples (vaulterup.com): 2.40x @5yr, 1.71x @10yr, 1.61x @15yr.
    for label, yrs in (("Underwritten", ASSUMPTIONS["hold_years_underwritten"]),
                       ("ActualHist",   ASSUMPTIONS["hold_years_actual"])):
        cols[f"IRR_at_{moic:g}x_{label}_{yrs}yr"] = round((moic ** (1 / yrs) - 1) * 100, 1)
    return _attach(df, cols)


# ─── Distress: the reason a good site is cheap ────────────────────────────────

def add_distress(df: pd.DataFrame) -> pd.DataFrame:
    """
    A senior partner's stated #1 rationale on the firm's largest recent
    acquisition was distressed basis -- bank REO at ~74% below the prior
    owner's basis. The old pipeline scored the same conditions as risk. Here
    they are upside.
    """
    dom = _num(_col(df, "Days On Market"))
    owner = (_text(_col(df, "Owner Name", default="")) + " " +
             _text(_col(df, "True Owner Name", default="")))
    last_price = _num(_col(df, "Last Sale Price"))
    ask = _num(_col(df, "For Sale Price"))

    basis_mult = ask / last_price.replace(0, pd.NA)

    distress_pat = r"bank|reo|foreclos|receiv|trustee|n\.a\.|credit union"
    lender_owned = owner.str.contains(distress_pat, case=False, regex=True, na=False)

    signals, scores = [], []
    for d, lo, bm in zip(dom, lender_owned, basis_mult):
        s, pts = [], 40
        if pd.notna(d) and d >= 730:
            s.append(f"{int(d)}d on market"); pts += 25
        elif pd.notna(d) and d >= 365:
            s.append(f"{int(d)}d on market"); pts += 15
        if lo:
            s.append("lender/REO-type owner"); pts += 25
        if pd.notna(bm) and bm < 1.0:
            s.append(f"asking {bm:.2f}x prior basis (BELOW)"); pts += 30
        elif pd.notna(bm) and bm < 2.0:
            s.append(f"asking {bm:.2f}x prior basis"); pts += 10
        signals.append("; ".join(s) if s else "none evident")
        scores.append(min(pts, 100))
    return _attach(df, {
        "Ask_vs_Prior_Basis": basis_mult.round(2),
        "Distress_Signals": signals,
        "_distress_score": scores,
    })


# ─── Cautions — surfaced, never eliminating ───────────────────────────────────

def add_cautions(df: pd.DataFrame) -> pd.DataFrame:
    """
    §7's "genuine cautions to surface". Deliberately does NOT include flood,
    missing entitlements, expired plats, railroad adjacency, easements or
    existing structures -- §5 documents the firm buying through every one of
    those. Flood is surfaced as an informational caution only, never a
    disqualifier -- see REBUILD_PLAN.md §7.4 for the history behind this.

    Also checks passed_on_patterns.py -- a documented pattern from the
    firm's own passed-on/lost-deal history (e.g. Weld County, CO oil & gas
    risk). Same rule as every other caution here: informational only, never
    changes Fit_Score or Fit_Tier, never removes a row. See that module's
    own docstring for why this table is small and hand-curated rather than
    mined from the full passed-on-deals file.
    """
    from analysis.screening.passed_on_patterns import passed_on_caution

    acres = _num(_col(df, "Land Area (AC)"))
    # NOTE: on both real exports `Floodplain Area` holds a LABEL, not a number
    # ("500-year Floodplain" on 69 of 216 Arizona rows), so this numeric read
    # yields NaN on every row and the share branch below has never once fired.
    # Quoting the label instead was tried and reverted: it fires on 161 of 216
    # rows, most of them 500-year, and it shadows the more precise signals
    # below. `In SFHA` is read directly (resolved 2026-07-29, REBUILD_PLAN.md
    # §7.4) because it agrees exactly with FEMA's own SFHA designation -- on
    # the Arizona file it matches `Flood Risk Area`'s "High Risk Areas" on the
    # same 46 rows -- and it's the only flood signal present at all on
    # templates (e.g. Dallas-Fort Worth) that carry no `Flood Risk Area`
    # column.
    flood_area = _num(_col(df, "Floodplain Area"))
    flood_risk = _text(_col(df, "Flood Risk Area", default=""))
    in_sfha = _text(_col(df, "In SFHA", default=""))
    stories = _num(_col(df, "Number of Stories"))
    ask = _num(_col(df, "For Sale Price"))
    state = _text(_col(df, "State", default=""))
    county = _text(_col(df, "County Name", default=""))

    out = []
    for ac, fa, fr, sf, st, pr, sta, co in zip(acres, flood_area, flood_risk, in_sfha,
                                                 stories, ask, state, county):
        c = []
        if pd.notna(fa) and pd.notna(ac) and ac > 0:
            share = fa / ac
            if share > 0.25:
                c.append(f"{share:.0%} of gross acreage in floodplain — price on NET acres")
        elif "high" in fr.lower():
            c.append("High flood-risk area — confirm net developable acreage")
        elif sf.strip().lower() == "yes":
            c.append("In a federal flood hazard area — historically not a dealbreaker for "
                      "the firm; verify site-specific mitigation cost")
        if pd.notna(st) and st > 0:
            c.append(f"{int(st)} structure(s) on site — verify if income or demo cost")
        large_ask_threshold = _COST.get("large_ask_threshold")
        large_ask_text = _COST.get("large_ask_reference_text")
        if large_ask_threshold and large_ask_text and pd.notna(pr) and pr > large_ask_threshold:
            c.append(f"${pr/1e6:.0f}M ask — {large_ask_text}")
        past = passed_on_caution(sta, co)
        if past:
            c.append(past)
        out.append("; ".join(c) if c else "")
    return _attach(df, {"Cautions": out})


def add_portfolio_comparison(df: pd.DataFrame) -> pd.DataFrame:
    """
    "Have we done anything like this before?" for every listing, not just
    market pricing. Deliberately separate from Fit_Score/Why -- this NEVER
    affects ranking or scoring, purely informational, same principle as
    Cautions above. See portfolio_comparison.py for what it does and does not
    compare (characteristics and history, never price, never a verdict).

    Loads the comparison index once for the whole export, not once per row --
    216 listings against a ~49-deal index is fast arithmetic either way, but
    there is no reason to re-read the same file 216 times.
    """
    from analysis.screening.portfolio_comparison import (
        compare_listing_row, load_index, summarize_match)

    index = load_index()
    if not index:
        return _attach(df, {"Portfolio_Comparison": [""] * len(df)})

    states = _text(_col(df, "State", default=""))
    counties = _text(_col(df, "County Name", default=""))
    kinds = _text(_col(df, "Secondary Type", default=""))
    acres = _num(_col(df, "Land Area (AC)"))

    out = []
    for st, co, kind, ac in zip(states, counties, kinds, acres):
        r = compare_listing_row(st, co, kind, ac, top_n=3, index=index)
        if not r["matches"]:
            out.append("")
            continue
        # What the firm DID and how it went, not just a name -- see
        # summarize_match. Two deals can match on geography and size and imply
        # opposite lessons (an entitlement play vs a finished-lot buy), which
        # the old name-and-outcome line could not express.
        parts = [summarize_match(m) for m in r["matches"]]
        out.append("Most similar in our history — " + " | ".join(parts))
    return _attach(df, {"Portfolio_Comparison": out})


# ─── Compose ──────────────────────────────────────────────────────────────────

# Tiers are assigned by RANK WITHIN THE EXPORT, not by an absolute score.
#
# Absolute cut-offs do not survive a change of market. They were calibrated on
# an Arizona file where the firm has 20 geocoded holdings and many listings sit
# inside a cluster. Run the same thresholds on Texas -- 4 holdings -- and almost
# nothing clears 75, so the tool reports "nothing to pursue" about a market the
# firm simply has not built out yet. That is a statement about the portfolio,
# not about the listings, and it is exactly the kind of silent mis-ranking this
# screener exists to avoid.
#
# Ranking within the batch always yields a workable shortlist. Fit_Score is kept
# alongside it so a genuinely weak batch is still visible: a Tier 1 at 82 and a
# Tier 1 at 54 mean very different things, and both are shown.
_TIER_BANDS = [(0.10, "1 — Pursue"), (0.35, "2 — Investigate"),
               (0.65, "3 — Watch"), (1.01, "4 — Low fit")]


def _assign_tiers(scores: pd.Series) -> pd.Series:
    """
    Tier each row by its percentile rank within this export (best = 0.0).

    If every row scores the same, there is no ranking to express and tiering
    anyway is actively misleading: percentile rank puts them ALL in the top
    band, so a file the screener could learn nothing from reports as fifty
    listings to pursue. Measured on a thin CoStar export template missing Land
    Area, Secondary Type, coordinates and days-on-market -- all 50 rows scored
    42 and all 50 came back Tier 1.

    Same rule as the neutral proximity floor and the untestable-pricing verdict:
    an uninformative input must abstain, never vote.
    """
    if scores.empty:
        return pd.Series(dtype=object)
    if scores.nunique() <= 1:
        return pd.Series("Unranked — nothing in this file separates them",
                         index=scores.index)
    # method="max", NOT "min". With min, every row in a tied group inherits the
    # rank of the group's FIRST member -- so 197 listings tied at the bottom of
    # a thin export took rank 20 and all landed in "1 - Pursue", putting the
    # whole file in the top tier. max gives the honest reading: how many
    # listings are at least as good as this one.
    pct = scores.rank(ascending=False, method="max", pct=True)
    return pct.map(lambda p: next(label for cut, label in _TIER_BANDS if p <= cut))


# Where the portfolio evidence actually comes from. Everything measured during
# the 2026-07-28 document review is Arizona, and most of it is Pinal County. A
# screen of a Texas or Colorado export must SAY so -- the alternative failures
# are applying Arizona figures where they don't belong, or marking a market down
# for being unfamiliar, which ranks the firm's own data coverage rather than the
# deals. See PORTFOLIO_STANDARD.md §1 and the neutral-floor note above.
_EVIDENCE_SCOPE = {
    "ARIZONA": "full — entitlement cost, horizontal cost, exit lot prices, "
               "schedule slip and 18 declined deals all on record",
    "CALIFORNIA": "partial — one entitlement budget (partial scope), one "
                  "completed sale (a loss), 5 declined deals. No horizontal "
                  "cost, no schedules, no exit prices",
}
_EVIDENCE_NONE = ("none — no cost, timing, exit-price or rejection record for this "
                  "state. The ranking is measured from this file alone")

# CoStar writes two-letter codes; property_coordinates.csv writes full names.
# Getting this wrong reported Arizona -- the one state with a full evidence
# record -- as having none, which is the most misleading output the function
# could produce. Normalise both sides to the full name.
_STATE_NAMES = {
    "AZ": "Arizona", "CA": "California", "TX": "Texas", "CO": "Colorado",
    "NM": "New Mexico", "NV": "Nevada", "UT": "Utah", "ID": "Idaho",
    "OR": "Oregon", "WA": "Washington", "OK": "Oklahoma", "FL": "Florida",
    "GA": "Georgia", "NC": "North Carolina", "SC": "South Carolina",
    "TN": "Tennessee", "AL": "Alabama", "AR": "Arkansas", "KS": "Kansas",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "WY": "Wyoming",
}


def _state_name(raw: str) -> str:
    """'AZ', 'az', 'Arizona' -> 'Arizona'. Unknown values pass through titled."""
    s = str(raw).strip()
    return _STATE_NAMES.get(s.upper(), s.title())


def _coverage(df: pd.DataFrame, holdings: pd.DataFrame) -> list[dict]:
    """
    What the portfolio can and cannot say about each state in this export.

    The ranking itself is computed from the file and travels anywhere. This
    reports the separate question of whether there is any history to interpret
    it against, so the reader is never left to assume there is.
    """
    states = sorted({_state_name(s) for s in _text(_col(df, "State", "State Name"))
                     if s and str(s).strip()})
    held = {}
    if not holdings.empty and "state" in holdings.columns:
        held = holdings["state"].map(_state_name).value_counts().to_dict()
    return [{
        "state": s,
        "holdings": int(held.get(s, 0)),
        "evidence": _EVIDENCE_SCOPE.get(s.upper(), _EVIDENCE_NONE),
    } for s in states]


# ─── Reading a listing against what Vaulter actually is ───────────────────────
# The ranking measures a listing against the other listings in its file. This
# measures it against the FIRM: what Vaulter does for a living, what it already
# owns near this spot, and which of its own deals this most resembles. None of
# it is scored -- a CoStar export cannot supply the things that actually decided
# the firm's best outcomes (who is selling, whether water is secured, whether
# the land is already platted), so this states them as questions rather than
# pretending to answer them. See docs/PORTFOLIO_STANDARD.md §3.

# What Vaulter is, in the terms that change how a listing should be read.
MISSION = ("Opportunistic value-add predevelopment land investor: buys raw or "
           "distressed land, carries it through entitlement, and sells the "
           "entitled position to users, developers and homebuilders — targeting "
           "2.5–3x on invested capital. Not a builder, not a spec developer.")

# How each land type actually exits, and what the firm's own record says it
# fetched. Residential figures are settlement statements; the $/sf figures are
# the firm's own models on live deals. The real reference figures live in the
# gitignored system/data/cost_assumptions.json; a generic shape-only sentence
# is used in its absence rather than a stale or invented number.
#
# Matched by PATTERN, in order, because `Proposed Land Use` does not use the
# five words this used to look for. Substring-matching "residential" left
# "Single Family Development", "Apartment Units", "MultiFamily", "Retail",
# "Medical", "Restaurant", "Convenience Store" and "Auto Dealership" -- 12 of
# 50 rows on the Tucson export -- all reading "exit product depends on what it
# gets entitled for", which is true of nothing in particular and says less than
# the export already did.
_EXIT_PATH = (
    (_RESIDENTIAL_PAT,
     _COST.get("exit_path_residential_text", "exits as lots")),
    (r"industrial|warehouse|distribution|manufactur|truck\s*stop|storage\s*yard",
     _COST.get("exit_path_industrial_text", "exits per sq ft")),
    (r"agricultur|pasture|ranch\b|farm|timber|open\s*space",
     "would exit as residential or commercial, not as farmland"),
    (r"commercial|retail|office|medical|health|restaurant|fast\s*food|hotel|"
     r"store|service\s*station|auto|bank|car\s*wash|mixed\s*use",
     _COST.get("exit_path_commercial_text", "exits per sq ft, not per lot")),
)
_EXIT_PATH_UNKNOWN = "exit product depends on what it gets entitled for"

# Asked once per screen, not repeated on all 216 rows -- these are the same
# three questions for every listing, and the export answers none of them.
PRE_PURSUIT_CHECKS = (
    "Before pursuing any of these, verify three things the export cannot show: "
    "who is selling and why (both of the firm's best outcomes were bought from "
    "banks — a federal receivership and a lender repossession), whether the land is "
    "already platted (three of the best were), and whether water is secured (in "
    "Pinal that decides whether a site is entitleable at all, and one agreement "
    "in the record took 18 months to sign)."
)

# Stage of the nearest holding, translated into what it means for a new listing
# in the same area.
_STAGE_MEANING = {
    "Acquisition":       "still being bought — the firm is actively adding here",
    "Pre-Plat":          "in early entitlement — the firm is working this area now",
    "Rezone":            "in rezoning — the firm is working this area now",
    "Final Engineering": "in late entitlement — the firm knows this jurisdiction well",
    "Development":       "in development",
    "Disposition":       "being sold — the firm is exiting here, not building up",
    "Site Maintenance":  "parked and not advancing — worth asking why before adding nearby",
}


def _holding_stages() -> dict:
    """Stage per holding name. Empty dict if the Project Master isn't readable."""
    try:
        from portfolio import load_properties
        return {p["name"]: p.get("category", "") for p in load_properties()[0]}
    except Exception as e:                      # a missing export must not break a screen
        log.warning(f"Could not read holding stages: {e}")
        return {}


def add_vaulter_context(df: pd.DataFrame) -> pd.DataFrame:
    """
    One line per listing reading it against the firm rather than against the file.

    Three parts, all short: how this type exits for a predevelopment investor,
    what the firm is doing at the nearest holding, and the questions the export
    cannot answer. Never scored -- this changes what a reader asks, not where a
    listing ranks.
    """
    stages = _holding_stages()
    kind_s = _text(_col(df, "Secondary Type", default="")).str.lower()
    acres = _num(_col(df, "Land Area (AC)"))

    notes = []
    for i in df.index:
        bits = []

        # How a predevelopment investor gets paid on this type.
        k = kind_s.loc[i]
        bits.append(next((text for pat, text in _EXIT_PATH if re.search(pat, k)),
                         _EXIT_PATH_UNKNOWN))

        # What the firm is already doing nearby, and what that stage implies.
        near = df.at[i, "Nearest_Holding"] if "Nearest_Holding" in df.columns else None
        dist = df.at[i, "Distance_Mi"] if "Distance_Mi" in df.columns else None
        if near and near == near and str(near) not in ("", "None"):
            stage = stages.get(str(near), "")
            meaning = _STAGE_MEANING.get(stage)
            if meaning and pd.notna(dist) and dist <= 25:
                bits.append(f"nearest holding {near} is {meaning}")

        # Scale, in the firm's own terms: subdivision is the value-add, so a
        # single buyer for the whole thing is the harder sale.
        a = acres.loc[i]
        if pd.notna(a) and a >= 100:
            bits.append("big enough to phase, and the firm's exits came from subdividing")

        line = "; ".join(bits)
        notes.append(line[:1].upper() + line[1:] + ".")

    return _attach(df, {"Vaulter_Read": notes})


def _why(row) -> str:
    """
    One plain sentence on why this listing sits where it does.

    Deliberately not a data dump -- every component already has its own column,
    and the detail view can show them. This is the line someone reads 216 times,
    so it is written for a reader, not a spreadsheet.
    """
    bits = []

    # Where it is, relative to what the firm owns.
    tier, dist = row["Cluster_Tier"], row.get("Distance_Mi")
    if tier not in ("Unknown", "New market") and pd.notna(dist):
        near = ("under a mile" if dist < 1 else
                "1 mile" if round(dist) == 1 else f"{dist:.0f} miles")
        bits.append(f"{near} from {row['Nearest_Holding']}")
    elif tier == "New market":
        bits.append("in a market where the firm owns nothing nearby")

    # Whether the money can work, in words rather than a ratio.
    h = row.get("Exit_Headroom")
    if pd.isna(h):
        # Which of the four causes, not all of them at once. "No asking price"
        # printed next to a visible asking price is the confusion this fixes.
        bits.append(_untestable_because(row.get("For Sale Price"),
                                        row.get("Land Area (AC)"),
                                        row.get("Exit_Comp_N")))
    elif h >= 2.0:
        bits.append(f"lots of room at this price ({h:.1f}x what it needs)")
    elif h >= 1.3:
        bits.append(f"clears the target comfortably ({h:.1f}x)")
    elif h >= 1.0:
        bits.append(f"only just clears the target ({h:.1f}x)")
    elif h >= 0.7:
        bits.append(f"falls short at this asking price ({h:.1f}x)")
    else:
        bits.append(f"priced as if the upside already happened ({h:.1f}x)")

    if pd.notna(h) and row.get("Pricing_Confidence") == "low":
        n = row.get("Exit_Comp_N")
        bits.append(f"but only {int(n)} comparable listing{'s' if n != 1 else ''} to judge that on")

    # Why it might be cheap -- the firm's strongest historical signal.
    if row["Distress_Signals"] != "none evident":
        bits.append(str(row["Distress_Signals"]).lower())

    # The one caveat that changes how the pricing line should be read. Only
    # worth saying where there IS a required exit to understate -- on a row with
    # no price or no size it followed "no asking price" with a caveat about a
    # number that was never computed.
    if (pd.notna(h) and isinstance(row.get("Cost_Basis"), str)
            and "understated" in row["Cost_Basis"]):
        bits.append("entitlement cost not included — no record for this type")

    # Uppercase the first letter only -- str.capitalize() would lowercase the
    # rest and turn "Example Trails" into "example trails".
    line = "; ".join(bits)
    return (line[:1].upper() + line[1:] + ".") if line else ""


def screen(source_path: Path, moic: float = None, write_workbook: bool = True) -> dict:
    """
    Rank a CoStar export by fit against the existing portfolio.

    Nothing is eliminated. Returns a dict with the ranked DataFrame, the
    assumptions used, and (if written) the workbook path.
    """
    moic = moic or ASSUMPTIONS["moic_target_high"]
    source_path = Path(source_path)

    # A broker's spreadsheet often opens with a title or blank line, so find the
    # real header rather than assuming the first row.
    hdr = _header_row(source_path)
    # skiprows, not header=: on a CSV, header=3 still makes pandas parse lines
    # 1-3 and fix the column count from the first of them, which then throws on
    # the real header row. Skipping drops them before parsing begins.
    df = (pd.read_excel(source_path, header=hdr)
          if source_path.suffix.lower() in (".xlsx", ".xls", ".xlsm")
          else pd.read_csv(source_path, skiprows=hdr))
    df = df.loc[:, [not str(c).startswith("Unnamed:") for c in df.columns]]
    log.info(f"Screening {len(df)} listings from {source_path.name} at {moic:g}x MOIC")

    # No two CoStar exports carry the same columns. Resolve every concept the
    # screener reads from whatever this file happens to provide, before anything
    # else touches it.
    df, column_sources = normalise_columns(df)
    for c in column_sources:
        if c["note"]:
            log.info(f"  {c['field']}: {c['note']} ({c['rows']} rows)")

    holdings = load_holdings()
    df = add_proximity(df, holdings)
    df = add_size_context(df)
    df = add_pricing(df, moic)
    df = add_distress(df)
    df = add_cautions(df)
    df = add_vaulter_context(df)
    df = add_portfolio_comparison(df)

    # If the firm owns nothing anywhere near this export, proximity carries no
    # information -- and scoring every listing down for it would make a genuinely
    # new market (a first Utah export, say) look uniformly bad, which is the
    # opposite of useful. Drop the weight to zero and let pricing, size and
    # distress decide, rather than penalising the whole file for being new.
    weights = dict(WEIGHTS)
    covered = (df["Cluster_Tier"] != "Unknown") & (df["Cluster_Tier"] != "New market")
    portfolio_coverage = float(covered.mean())
    if portfolio_coverage < 0.05:
        weights["proximity"] = 0
        log.info("No holdings near this market — proximity excluded from the score.")

    total = sum(weights.values())
    fit = ((
        df["_proximity_score"] * weights["proximity"]
        + df["_pricing_score"] * weights["pricing"]
        + df["_distress_score"] * weights["distress"]
        + df["_size_score"] * weights["size_fit"]
    ) / total).round(1)
    # Publish the four components alongside the composite. A single score is
    # not arguable; "84.8 because it is 4mi from an existing holding and
    # clears its exit at 2.7x" is. The whole premise of the standard being
    # readable depends on being able to see what drove a number.
    df = _attach(df, {
        "Fit_Score": fit,
        "Fit_Tier": _assign_tiers(fit),
        "Score_Proximity": df["_proximity_score"],
        "Score_Pricing": df["_pricing_score"],
        "Score_Distress": df["_distress_score"],
        "Score_Size": df["_size_score"],
    })
    df = _attach(df, {"Why": df.apply(_why, axis=1)})

    df = df.sort_values("Fit_Score", ascending=False).reset_index(drop=True).copy()
    df.insert(0, "Rank", range(1, len(df) + 1))

    front = ["Rank", "Fit_Tier", "Fit_Score",
             "Score_Proximity", "Score_Pricing", "Score_Distress", "Score_Size",
             "Property Address", "City", "State",
             "Secondary Type", "Land Area (AC)", "For Sale Price", "Ask_Per_Acre",
             "Nearest_Holding", "Distance_Mi", "Cluster_Tier", "Size_Context",
             "Size_Band", "Exit_As_Band", "Cost_Basis",
             "Entitlement_Per_Acre", "Carry_Per_Acre", "Required_Exit_Per_Acre",
             "Exit_Comp_Per_Acre", "Exit_Headroom", "Pricing_Confidence",
             "Exit_Comp_N", "Exit_Comp_Basis", "Pricing_Verdict",
             "Distress_Signals", "Ask_vs_Prior_Basis",
             "Cautions", "Why", "Vaulter_Read", "Portfolio_Comparison", "Days On Market",
             "Indicative_Lots", "Implied_Exit_Per_Lot"]
    front = [c for c in front if c in df.columns]
    view = df[front + [c for c in df.columns if c not in front and not c.startswith("_")]]

    result = {
        "source": source_path.name,
        "total_screened": len(df),
        "moic_target": moic,
        "tier_counts": df["Fit_Tier"].value_counts().sort_index().to_dict(),
        "markets": sorted(_col(df, "Market Name").dropna().astype(str).unique().tolist()),
        "holdings_used": len(holdings),
        "portfolio_coverage": round(portfolio_coverage, 3),
        "evidence_coverage": _coverage(df, holdings),
        "column_sources": column_sources,
        "weights_used": weights,
        "assumptions": dict(ASSUMPTIONS),
        "exit_lot_comps": list(EXIT_LOT_COMPS),
        "dataframe": view,
    }

    if write_workbook:
        # One workbook per source file, overwritten each run -- not timestamped.
        # Confirmed 2026-07-29: this used to accumulate 149 files in the shared
        # OneDrive folder for 3 source files re-screened repeatedly during
        # development. Same fix already applied to pipeline/proximity_tool.py.
        out = Path(SCREENING_OUTPUT_DIR) / f"fit_screen_{source_path.stem}.xlsx"
        out.parent.mkdir(parents=True, exist_ok=True)
        with pd.ExcelWriter(out, engine="openpyxl") as xl:
            view.to_excel(xl, sheet_name="Ranked", index=False)
            pd.DataFrame(
                [{"assumption": k, "value": str(v)} for k, v in ASSUMPTIONS.items()]
                + [{"assumption": f"weight_{k}", "value": str(v)} for k, v in WEIGHTS.items()]
            ).to_excel(xl, sheet_name="Assumptions", index=False)
            # What the portfolio can and cannot say about these markets, and
            # what finished lots have actually fetched. Both are context the
            # ranking deliberately does not fold into a score.
            pd.DataFrame(result["evidence_coverage"]).to_excel(
                xl, sheet_name="Evidence Coverage", index=False)
            pd.DataFrame([{"buyer": b, "date": d, "price_per_lot": p}
                          for b, d, p in EXIT_LOT_COMPS]).to_excel(
                xl, sheet_name="Exit Lot Comps", index=False)
        result["workbook_path"] = str(out)
        log.info(f"Wrote {out}")

    return result
