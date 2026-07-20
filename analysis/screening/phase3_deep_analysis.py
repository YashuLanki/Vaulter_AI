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

import pandas as pd
import anthropic

TOP_N_DEFAULT = 10

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


def build_prompt(row: pd.Series, scoreboard: str, rank: int, top_n: int = TOP_N_DEFAULT) -> str:
    full_record = build_full_record_text(row)
    reasons = row.get("Screening_Reasons", "") or "(none -- passed clean)"

    return f"""You are a land acquisition analyst at Vaulter, reviewing a
top-ranked candidate for potential pursuit.

INVESTMENT THESIS:
{INVESTMENT_THESIS}

THIS LISTING'S RANK: #{rank} of {top_n} in this batch.

SCOREBOARD -- all {top_n} listings in this batch, for relative context:
{scoreboard}

FULL RAW DATA FOR THIS LISTING:
{full_record}

PHASE 1 SCREENING NOTES (any flags this listing picked up before ranking):
{reasons}

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


def run_deep_analysis(ranked_df: pd.DataFrame, api_key: str, top_n: int = TOP_N_DEFAULT) -> dict:
    """
    Runs Claude once per top-N listing, exactly as costar's original
    deep_analysis.py did (same prompt construction, same max_tokens=1500).
    Returns {address: {section_name: text, "Composite_Score": float}}.
    Does NOT write any xlsx file -- that happens later in
    workbook_builder.build_combined_workbook.
    """
    top_listings = get_top_listings(ranked_df, top_n)
    scoreboard = build_scoreboard(top_listings)
    client = anthropic.Anthropic(api_key=api_key)

    results = {}
    for i, (_, row) in enumerate(top_listings.iterrows(), 1):
        addr = row.get("Property Address", f"Listing_{i}")
        prompt = build_prompt(row, scoreboard, i, top_n=len(top_listings))
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text
        parsed = parse_response(text)
        parsed["Composite_Score"] = row["Composite_Score"]
        results[addr] = parsed

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
