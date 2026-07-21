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
import json
import logging
from pathlib import Path

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


def get_top_listings(ranked_df: pd.DataFrame, top_n: int = TOP_N_DEFAULT) -> pd.DataFrame:
    """ranked_df is already Phase-1 + Phase-2 processed and sorted by
    Composite_Score descending (see phase2_ranking.rank_listings) -- this
    is just a head(top_n), no re-running of upstream phases."""
    return ranked_df.head(top_n)


def build_scoreboard(top_listings: pd.DataFrame) -> str:
    lines = []
    for i, (_, row) in enumerate(top_listings.iterrows(), 1):
        addr = row.get("Property Address", "Unknown")
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
        matched = False
        for key in sections:
            if stripped.startswith(f"{key}:"):
                current = key
                remainder = stripped[len(key) + 1:].strip()
                if remainder:
                    sections[key].append(remainder)
                matched = True
                break
        if not matched and current and stripped:
            sections[current].append(stripped)
    return {k: "\n".join(v) for k, v in sections.items()}


def _cache_key(full_record_text: str) -> str:
    return hashlib.sha256((PROMPT_VERSION + full_record_text).encode()).hexdigest()


def _load_cache(cache_path: Path) -> dict:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            return {}
    return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.write_text(json.dumps(cache, indent=2))


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
    cache = _load_cache(cache_path) if cache_path else {}
    cache_hits = 0

    results = {}
    for i, (_, row) in enumerate(top_listings.iterrows(), 1):
        addr = row.get("Property Address", f"Listing_{i}")
        key = _cache_key(_cacheable_record_text(row))

        if key in cache:
            parsed = dict(cache[key])
            cache_hits += 1
        else:
            content_blocks = build_prompt(row, scoreboard, i, top_n=len(top_listings))
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{"role": "user", "content": content_blocks}],
            )
            text = response.content[0].text
            parsed = parse_response(text)
            if cache_path:
                cache[key] = dict(parsed)
                _save_cache(cache_path, cache)

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
