"""
Surfaces a documented pattern from the firm's own passed-on-deal history as a
CAUTION on a new listing -- never a score change, never an elimination.

Read this alongside portfolio_comparison.py, which it's a close cousin of but
deliberately separate from: that module compares a listing to the firm's own
completed deals (what it bought and what happened). This one compares a
listing to what the firm has PASSED ON or LOST, sourced from
Vaulter AI Shared/property_summaries/_passed-on-deals.md.

## Why this is a short, explicit table, not a pattern-miner over that file

That file is honest about its own limits: for most of the ~45 dead deals it
documents, WHY the deal died is not confirmed by any readable document --
only that it did. Auto-deriving "patterns" from prose with mostly-uncertain
causation would manufacture exactly the kind of unverified, silently-wrong
signal this project has spent real effort avoiding elsewhere (the Overpass
mirror that answered a flood question with a confident, structurally valid,
completely wrong "no results"; a hard filter that once eliminated 60 of 69
real listings on grounds that weren't real dealbreakers). So KNOWN_PATTERNS
below is deliberately curated by hand, the same way fit_screen.py's own
ASSUMPTIONS and WEIGHTS are: a short, sourced, arguable list a partner can
add to or dispute, not a black box. A pattern goes in this table only when
MULTIPLE deals in the same place share a REAL documented cause -- one
unconfirmed "reason not documented" deal is not a pattern.

## The one rule that must never break

Everything this returns is a CAUTION -- informational text appended
alongside flood/structure/price cautions in add_cautions(). It must never
change Fit_Score, Fit_Tier, or remove a row. A past "no" is color for a
conversation, never an automated screen input -- see _passed-on-deals.md's
own opening warning, word for word the same rule.
"""

# Each entry: which state+county it applies to (matched loosely, same
# substring-on-normalized-text approach as portfolio_comparison.py, since a
# CoStar export's own county field is free text, not a fixed code), the
# caution text to surface, and where the evidence lives so it can be
# checked rather than taken on faith.
KNOWN_PATTERNS = [
    {
        "id": "weld_county_co_oilgas",
        "state": "CO",
        "county": "Weld",
        "caution": (
            "Multiple of the firm's own passed-on Weld County, CO deals document "
            "active or legacy oil & gas wells, leases, or environmental contamination as "
            "a real complication -- worth a targeted mineral-rights and Phase I check "
            "before proceeding. Past pattern, not a confirmed cause for any one deal; "
            "context, not a reason to avoid this one."
        ),
        "source": "_passed-on-deals.md, Colorado section",
    },
]


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def passed_on_caution(state, county) -> str | None:
    """
    Returns a caution string if (state, county) matches a documented pattern
    from the firm's own passed-on-deal history, else None. Never raises --
    a blank or unrecognized state/county is normal input, not an error.
    """
    s_norm = _norm(state)
    c_norm = _norm(county)
    if not s_norm or not c_norm:
        return None
    for pattern in KNOWN_PATTERNS:
        if _norm(pattern["state"]) == s_norm and _norm(pattern["county"]) in c_norm:
            return pattern["caution"]
    return None
