"""
A short, hand-authored timeline of US macro/housing-market eras, covering the span
of the firm's own deal history (1999-2026). Built for the portfolio comparison
matcher (portfolio_comparison.py) so a past deal's timing can be read in context
-- "bought during the post-crash bottom" means something different from "bought
at a 2021 peak" -- without asking every property summary to re-research and
re-cite the same public history.

This is deliberately separate from PROPERTY_HISTORY in portfolio_comparison.py.
Everything in there is cited to a specific document in the firm's own files.
Everything here is general public-record economic history -- recession dates,
rate-cycle turns -- which isn't a claim about any one deal and shouldn't be
mixed with claims that are. Dates are standard, publicly documented benchmarks
(NBER recession dating, well-known Federal Reserve rate-cycle history), not
independently re-verified per year here.

Deliberately coarse: this exists to say "these two deals were bought in
similar/different macro moments," not to model the economy. If a property's own
summary already quotes contemporaneous market conditions from a firm document
(most do -- these memos routinely cite the market they were reacting to), that
cited, deal-specific figure always wins over this general reference.
"""

# (start_year, end_year_inclusive, label, one-line public-record description)
ERAS = [
    (1999, 2001, "Late-90s expansion / dot-com peak & bust",
     "Strong growth into 2000, then the dot-com bust and a mild 2001 recession (NBER: Mar-Nov 2001)."),
    (2002, 2006, "Housing boom",
     "Post-9/11 rate cuts fed a multi-year housing boom; prices in many Sun Belt markets peaked around 2005-2006."),
    (2007, 2009, "Housing bust / global financial crisis",
     "Subprime collapse, widespread foreclosures, NBER recession Dec 2007-Jun 2009 -- the deepest downturn since the 1930s."),
    (2010, 2012, "Post-crash bottom",
     "Distressed/REO inventory dominated the market; the Fed held rates near zero (ZIRP from Dec 2008); this is the "
     "environment several of the firm's own 2011-2013 deal memos describe as bank-REO buying opportunities."),
    (2013, 2015, "Recovery",
     "Home prices and homebuilder demand recovering broadly off the 2011-2012 bottom; several firm memos from this "
     "window explicitly cite a returning \"lot crisis\" / builder demand narrative."),
    (2016, 2019, "Expansion",
     "Broad economic and housing expansion; the Fed raised rates gradually through 2018 before pausing/cutting in 2019."),
    (2020, 2020, "COVID shock",
     "Sharp, brief recession (NBER: Feb-Apr 2020) followed by an unusual demand surge in housing as rates were cut "
     "back to near zero."),
    (2021, 2021, "Pandemic-era low-rate boom",
     "Near-zero rates, low inventory, and remote-work migration drove rapid price appreciation and heavy demand; "
     "construction material and labor shortages were widely reported."),
    (2022, 2023, "Rapid rate-hike cycle",
     "The Fed raised rates at the fastest pace in decades to fight inflation; mortgage rates roughly doubled, and "
     "housing activity slowed broadly from the 2021 peak."),
    (2024, 2026, "Current: elevated-but-stabilizing rates",
     "Rates well above the 2010s norm but no longer rising sharply; several 2025-2026 firm asset updates describe "
     "individual submarkets as buyer's markets with softening prices and rising months-of-supply."),
]


def era_for_year(year) -> dict | None:
    """
    Returns the era covering `year`, or None if year is outside 1999-2026 or
    unparseable. Never raises -- a bad/missing year is a normal input here,
    not an error condition, since not every deal has a confirmed entry year.
    """
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None
    for start, end, label, desc in ERAS:
        if start <= y <= end:
            return {"start": start, "end": end, "label": label, "description": desc}
    return None


def era_note(year) -> str:
    """
    One-line, ready-to-print note for a given entry year, or "" if the year
    is unknown/out of range -- callers should just omit the line rather than
    print a placeholder.
    """
    era = era_for_year(year)
    if not era:
        return ""
    return f"Bought during: {era['label']} ({era['start']}-{era['end']}) -- {era['description']}"
