"""
analysis/screening/phase3_deep_analysis.py
---------------------------------------------
Vaulter CoStar Listing Screener — Phase 3 Deep Analysis

Takes the already-ranked dataframe (Phase 1 + Phase 2 already applied by
the pipeline orchestrator) and sends each of the top N listings to Claude
for a qualitative analysis -- using the FULL raw CoStar record for that
listing (not just the trimmed display columns), plus its scores and a
scoreboard of all N listings for relative context.

Cost: ~10 Claude API calls total, negligible (~$0.15-0.25 for the full
batch at current Sonnet pricing).
"""

import hashlib
import logging
from pathlib import Path

import safe_io

import pandas as pd
import anthropic

log = logging.getLogger("vaulter.screening")

TOP_N_DEFAULT = 15

# Bump this if build_prompt's format changes meaningfully -- it's folded
# into the cache key so old cached analyses (in a different format) don't
# get served against a changed prompt/parser.
PROMPT_VERSION = "v1"

# Columns excluded from what Claude sees -- contact/personal info that
# adds no analytical value and shouldn't be piped into an AI prompt.
EXCLUDE_FROM_ANALYSIS = {
    "Owner Phone", "Owner Contact", "Owner Address", "Owner City State Zip",
    "Recorded Owner Phone", "Recorded Owner Contact", "Recorded Owner Address",
    "Recorded Owner City State Zip", "True Owner Phone", "True Owner Contact",
    "True Owner Address", "True Owner City State Zip",
    "Sale Company Contact", "Sale Company Phone", "Sale Company Fax",
    "Sale Company Address", "Sale Company City State Zip",
    "Sales Contact", "Sales Contact Phone", "Primary Agent Name",
    "Leasing Company Contact", "Leasing Company Phone", "Leasing Company Fax",
    "Leasing Company Address", "Leasing Company City State Zip",
    "Property Manager Contact", "Property Manager Phone",
    "Property Manager Address", "Property Manager City State Zip",
    "_Screening_Key",  # internal dedup key (see _add_unique_keys) -- not real listing data
}

# Columns computed by THIS pipeline run (Phase 1/2), not part of the raw
# CoStar record -- excluded from the cache key specifically (but NOT from
# what Claude sees in the prompt, via build_full_record_text). These are
# recalculated fresh from percentile rank / dynamic IQR thresholds across
# the WHOLE batch every run, so the exact same physical listing can get
# different values here purely because other rows in the file changed --
# hashing them defeats the cache for a listing whose own data is identical.
EXCLUDE_FROM_CACHE_KEY = {
    "Screening_Status", "Screening_Reasons", "Flag_Count",
    "Score_DaysOnMarket", "Score_Price", "Score_LandUseCategory",
    "Score_DevelopedEnv", "Score_FloodRisk", "Composite_Score",
}

INVESTMENT_THESIS = """
Vaulter is an opportunistic, value-add land investment firm focused on
predevelopment value-add across AZ, CA, CO, NM, and TX. Vaulter expects to
achieve 2.5x-3x+ MOIC by entitling/improving raw land and selling to end
users/developers -- not by evaluating price against simple market comps.
In scope: Industrial, Residential, and Mixed-use land (Agricultural also
acceptable, but a weaker fit). Land already trending toward conventional
retail/hospitality/commercial-pad development is a weaker fit -- the
opposite of raw predevelopment upside.
"""


def _add_unique_keys(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add a `_Screening_Key` column: the Property Address, disambiguated
    with a "(#2)", "(#3)"... suffix for any address that repeats within
    this dataframe.

    Two different listings sharing the same address (multiple parcels at
    one street address, or several rows with a blank/generic address) are
    a real possibility in a CoStar export. Every stage downstream
    (Phase 3's per-listing results, Phase 4's finalist selection, the
    combined workbook's matrix-sheet columns) keys a dict by this value --
    without disambiguating it first, the second listing's analysis would
    silently overwrite the first's under the identical key, and one whole
    listing's worth of Claude analysis (which was still paid for) would
    just vanish from the output. Built once here and reused everywhere so
    every stage agrees on the same key for the same row.
    """
    df = df.copy()
    addresses = df["Property Address"] if "Property Address" in df.columns else pd.Series([None] * len(df), index=df.index)

    counts = {}
    keys = []
    for i, val in enumerate(addresses.tolist(), start=1):
        base = str(val).strip() if val is not None and str(val).strip() and str(val).strip().lower() != "nan" else f"Listing_{i}"
        counts[base] = counts.get(base, 0) + 1
        keys.append(base if counts[base] == 1 else f"{base} (#{counts[base]})")

    df["_Screening_Key"] = keys
    return df


def get_top_listings(ranked_df: pd.DataFrame, top_n: int = TOP_N_DEFAULT) -> pd.DataFrame:
    """ranked_df is already Phase-1 + Phase-2 processed and sorted by
    Composite_Score descending (see phase2_ranking.rank_listings) -- this
    is just a head(top_n), no re-running of upstream phases. Adds
    _Screening_Key (see _add_unique_keys) so every downstream stage has a
    guaranteed-unique key to use instead of the raw, possibly-duplicated
    Property Address."""
    return _add_unique_keys(ranked_df.head(top_n))


def build_scoreboard(top_listings: pd.DataFrame) -> str:
    lines = []
    for i, (_, row) in enumerate(top_listings.iterrows(), 1):
        addr = row.get("_Screening_Key", row.get("Property Address", "Unknown"))
        lines.append(
            f"#{i} {addr} | Composite: {row['Composite_Score']} | "
            f"DaysOnMarket score: {row['Score_DaysOnMarket']:.0f} | "
            f"Price score: {row['Score_Price']:.0f} | "
            f"LandUseCategory score: {row['Score_LandUseCategory']:.0f} | "
            f"DevelopedEnv score: {row['Score_DevelopedEnv']:.0f} | "
            f"FloodRisk score: {row['Score_FloodRisk']:.0f}"
        )
    return "\n".join(lines)


def build_full_record_text(row: pd.Series) -> str:
    lines = []
    for col, val in row.items():
        if col in EXCLUDE_FROM_ANALYSIS:
            continue
        if pd.isna(val):
            continue
        lines.append(f"{col}: {val}")
    return "\n".join(lines)


def _cacheable_record_text(row: pd.Series) -> str:
    """Like build_full_record_text, but ALSO excludes the batch-dependent
    Phase 1/2 derived columns (see EXCLUDE_FROM_CACHE_KEY) -- used only for
    computing the cache key, never for what Claude actually sees."""
    lines = []
    for col, val in row.items():
        if col in EXCLUDE_FROM_ANALYSIS or col in EXCLUDE_FROM_CACHE_KEY:
            continue
        if pd.isna(val):
            continue
        lines.append(f"{col}: {val}")
    return "\n".join(lines)


def build_prompt(row: pd.Series, scoreboard: str, rank: int, top_n: int = TOP_N_DEFAULT) -> list[dict]:
    """
    Returns Anthropic content blocks (not a single string) split into:
      - a STATIC block (instructions, investment thesis, scoreboard, output
        format) that is IDENTICAL across every listing in this batch --
        marked cache_control so Anthropic caches it server-side after the
        first call, billing the ~top_n-1 remaining calls in this batch far
        less for that shared portion (see run_deep_analysis).
      - a DYNAMIC block (this listing's own rank/raw data/flags) that
        genuinely differs per call and is never cached.
    """
    static_block = f"""You are a land acquisition analyst at Vaulter, reviewing a
top-ranked candidate for potential pursuit.

INVESTMENT THESIS:
{INVESTMENT_THESIS}

SCOREBOARD -- all {top_n} listings in this batch, for relative context:
{scoreboard}

Write your analysis in EXACTLY this format, with these 5 section headers
verbatim (all caps, followed by a colon). Under each header, write 3-5
CONCISE BULLET POINTS (each starting with "- "), not paragraphs. Each
bullet should carry real information -- specific numbers, zoning codes,
dollar amounts, timeframes -- not vague filler. Do not add any other
headers or preamble.

Under RECOMMENDATION specifically, the FIRST line must be exactly one of
the following three lines, with no other text on that line:
VERDICT: Pursue
VERDICT: Conditional
VERDICT: Pass
Choose exactly one. This line is parsed by code to sort listings, so it
must match one of those three lines verbatim -- do not paraphrase it or
add extra words. Follow it with your bullets explaining the verdict.

STRENGTHS_AND_RISKS:
- <bullet>
- <bullet>

ENTITLEMENT_RISK:
- <bullet>
- <bullet>

RECOMMENDATION:
VERDICT: <Pursue, Conditional, or Pass>
- <bullet explaining the verdict>
- <bullet>

MOIC_FIT:
- <bullet>
- <bullet>

RED_FLAGS:
- <bullet>
- <bullet>
"""

    full_record = build_full_record_text(row)
    reasons = row.get("Screening_Reasons", "") or "(none -- passed clean)"
    dynamic_block = f"""THIS LISTING'S RANK: #{rank} of {top_n} in this batch.

FULL RAW DATA FOR THIS LISTING:
{full_record}

PHASE 1 SCREENING NOTES (any flags this listing picked up before ranking):
{reasons}

Now write your analysis for the listing above, in the exact format specified."""

    return [
        {"type": "text", "text": static_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": dynamic_block},
    ]


def normalize_header_line(line: str) -> str:
    """
    Strip markdown heading/emphasis wrapping from a line before checking
    whether it's one of the required section headers.

    The prompt asks Claude for headers verbatim (e.g. "RECOMMENDATION:"),
    but models sometimes wrap them in markdown anyway -- "**RECOMMENDATION:**",
    "**RECOMMENDATION**:", "# RECOMMENDATION:", "__RECOMMENDATION:__". A raw
    `line.startswith(f"{key}:")` check fails on all of these, silently
    misfiling that whole section's content into whatever the previous
    section was (or dropping it entirely if it's the very first header,
    since `current` is still None). This normalized value is used only
    for the header match itself -- the original line (with any legitimate
    markdown in bullet content) is still what gets stored.
    """
    s = line.strip().lstrip("#").strip()
    # "**"/"__" are always markdown bold wrapping, never part of a key
    # name, so stripping them anywhere is safe. A single leading/trailing
    # "*" or "_" (italic wrapping, or bold split around the colon like
    # "**KEY**:") is only stripped from the very ends -- key names like
    # STRENGTHS_AND_RISKS use "_" as an internal separator, so a global
    # strip would wrongly mangle the key itself.
    s = s.replace("**", "").replace("__", "")
    return s.strip("*_").strip()


def parse_response(text: str) -> dict:
    sections = {
        "STRENGTHS_AND_RISKS": [],
        "ENTITLEMENT_RISK": [],
        "RECOMMENDATION": [],
        "MOIC_FIT": [],
        "RED_FLAGS": [],
    }
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        header_line = normalize_header_line(stripped)
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


def _cache_key(full_record_text: str) -> str:
    return hashlib.sha256((PROMPT_VERSION + full_record_text).encode()).hexdigest()


def run_deep_analysis(ranked_df: pd.DataFrame, api_key: str, top_n: int = TOP_N_DEFAULT,
                       cache_dir: Path | None = None) -> dict:
    """
    Runs Claude once per top-N listing, exactly as costar's original
    deep_analysis.py did (same prompt construction, same max_tokens=1500).
    Returns {address: {section_name: text, "Composite_Score": float}}.
    Does NOT write any xlsx file -- that happens later in
    workbook_builder.build_combined_workbook.

    If cache_dir is given (the pipeline passes SCREENING_OUTPUT_DIR, which
    is shared across the whole team), each listing's analysis is cached by
    a hash of its own record, EXCLUDING the Phase 1/2 derived columns (see
    EXCLUDE_FROM_CACHE_KEY) -- so if the SAME listing reappears in a later
    CoStar export (a re-list, or a file with a few new/changed rows), it's
    reused instead of re-paying for that listing's Claude call, even
    though its Score_*/Composite_Score/Screening_Reasons would otherwise
    differ slightly just from the batch composition changing. An
    acceptable tradeoff since the listing's own raw record is what
    actually drives the analysis content.
    """
    top_listings = get_top_listings(ranked_df, top_n)
    scoreboard = build_scoreboard(top_listings)
    client = anthropic.Anthropic(api_key=api_key)

    cache_path = (cache_dir / "phase3_listing_cache.json") if cache_dir else None
    # A one-time snapshot read is fine here -- it's just for checking
    # existing entries, and a slightly stale read only costs an occasional
    # avoidable Claude call, not data loss. The WRITE side below is what
    # matters: each new entry is merged into whatever's on disk at that
    # moment (via locked_json_update), not overwritten from this snapshot,
    # so another team member's concurrent additions to this same shared
    # file are never clobbered.
    cache = safe_io.load_json(cache_path) if cache_path else {}
    cache_hits = 0

    results = {}
    for i, (_, row) in enumerate(top_listings.iterrows(), 1):
        addr = row.get("_Screening_Key", row.get("Property Address", f"Listing_{i}"))
        key = _cache_key(_cacheable_record_text(row))

        if key in cache:
            parsed = dict(cache[key])
            cache_hits += 1
        else:
            try:
                content_blocks = build_prompt(row, scoreboard, i, top_n=len(top_listings))
                response = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=1500,
                    messages=[{"role": "user", "content": content_blocks}],
                )
                if not response.content:
                    raise ValueError("Claude returned an empty response (no content blocks)")
                text = response.content[0].text
                parsed = parse_response(text)
                if cache_path:
                    safe_io.locked_json_update(cache_path, lambda current, k=key, p=parsed: {**current, k: dict(p)})
            except Exception as e:
                # One bad/empty Claude response must not abort the whole
                # batch -- every OTHER listing in this run (including any
                # after this one) still deserves its analysis. Record a
                # clearly-flagged placeholder instead so this listing is
                # still visible in the workbook for manual follow-up,
                # rather than either crashing the run or silently vanishing.
                log.error(f"  Phase 3 analysis failed for '{addr}': {e}")
                parsed = {
                    "STRENGTHS_AND_RISKS": "", "ENTITLEMENT_RISK": "",
                    "RECOMMENDATION": f"ANALYSIS FAILED -- needs manual review ({e})",
                    "MOIC_FIT": "", "RED_FLAGS": "",
                }

        parsed["Composite_Score"] = row["Composite_Score"]
        results[addr] = parsed

    if cache_path and cache_hits:
        log.info(f"Phase 3: reused {cache_hits}/{len(top_listings)} cached listing "
                  f"analyses -- no new Claude calls for those.")

    return results


if __name__ == "__main__":
    # Thin manual-test CLI wrapper -- the real entry point is
    # run_deep_analysis() called from analysis/screening/pipeline.py.
    import os
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not path or not api_key:
        print("Usage: ANTHROPIC_API_KEY=... python -m analysis.screening.phase3_deep_analysis <path_to_costar_export.xlsx>")
        sys.exit(1)

    from . import phase1_rules, phase2_ranking

    df = pd.read_excel(path)
    scored = phase1_rules.run_screener(df)
    ranked = phase2_ranking.rank_listings(scored)
    analyses = run_deep_analysis(ranked, api_key)
    print(list(analyses.keys()))
