"""
analysis/screening/phase2_ranking.py
--------------------------------------
Vaulter CoStar Listing Screener — Phase 2 Ranking

Takes the ALREADY Phase-1-processed dataframe (has Screening_Status /
Screening_Reasons / Flag_Count columns) and ranks the Phase 1 survivors
using a weighted composite score across 5 dimensions. No re-reading from
disk and no re-running Phase 1 here -- the pipeline orchestrator
(analysis/screening/pipeline.py) is responsible for calling Phase 1 first
and passing its output in.

Days On Market and Price are scored by percentile rank (fully dynamic --
recalculated fresh from whatever data is loaded, no hardcoded numbers).
Land-use fit, developed-environment penalty, and flood risk severity are
scored using scoring_config.py (in this same package), which is approved
after running scoring_generator.py -- never assumed in advance.

Dimension weights are also dynamic: calculated from how complete
(non-null) each dimension's underlying data actually is for this export.
"""

import logging

import pandas as pd

from . import scoring_config

log = logging.getLogger("vaulter.screening")


def percentile_score(series: pd.Series, higher_is_better: bool = False) -> pd.Series:
    """0-100 score based on percentile rank within the given series.
    higher_is_better=False means lower raw values score higher (used for
    Days On Market and Price, where less is more desirable)."""
    ranks = series.rank(pct=True, na_option="keep")
    if not higher_is_better:
        ranks = 1 - ranks
    return (ranks * 100).fillna(50)  # missing -> neutral midpoint


def score_land_use_category(df: pd.DataFrame) -> pd.Series:
    mapping = scoring_config.SCORING_MAP["secondary_type_scores"]
    default = 50
    scores = []
    for val in df["Secondary Type"]:
        if pd.isna(val):
            scores.append(default)
        elif val in mapping:
            scores.append(mapping[val])
        else:
            log.warning(f"  '{val}' not in scoring_config secondary_type_scores, using default {default}")
            scores.append(default)
    return pd.Series(scores, index=df.index)


def score_developed_environment(df: pd.DataFrame) -> pd.Series:
    mapping = scoring_config.SCORING_MAP["land_use_penalty_tags"]
    default_penalty = 0
    scores = []
    for val in df["Proposed Land Use"]:
        if pd.isna(val):
            scores.append(70)
            continue
        tags = [t.strip() for t in str(val).split(",") if t.strip()]
        if not tags:
            scores.append(70)
            continue
        penalties = []
        for tag in tags:
            if tag in mapping:
                penalties.append(mapping[tag])
            else:
                log.warning(f"  tag '{tag}' not in scoring_config land_use_penalty_tags, using default {default_penalty}")
                penalties.append(default_penalty)
        avg_penalty = sum(penalties) / len(penalties)
        scores.append(100 - avg_penalty)
    return pd.Series(scores, index=df.index)


def score_flood_risk(df: pd.DataFrame) -> pd.Series:
    mapping = scoring_config.SCORING_MAP["flood_risk_scores"]
    default = 70
    scores = []
    for val in df["Flood Risk Area"]:
        if pd.isna(val):
            scores.append(default)
        elif val in mapping:
            scores.append(mapping[val])
        else:
            log.warning(f"  '{val}' not in scoring_config flood_risk_scores, using default {default}")
            scores.append(default)
    return pd.Series(scores, index=df.index)


def compute_dynamic_weights(df: pd.DataFrame) -> dict:
    completeness = {
        "days_on_market": df["Days On Market"].notna().mean(),
        "land_use_category": df["Secondary Type"].notna().mean(),
        "price": df["For Sale Price"].notna().mean(),
        "developed_environment": df["Proposed Land Use"].notna().mean(),
        "flood_risk": df["Flood Risk Area"].notna().mean(),
    }
    total = sum(completeness.values())
    if not total or pd.isna(total):
        # Every one of these 5 columns is completely empty across every
        # surviving row (or there are no surviving rows at all) -- dividing
        # by zero here would silently turn every weight, and therefore
        # every listing's Composite_Score, into NaN. That corrupts Phase 2
        # ranking outright and cascades into Phase 3's top-N selection,
        # Phase 4 tiering, and the workbook's Composite_Score column, all
        # of which trust Composite_Score to be a real number. Fall back to
        # equal weighting across all 5 dimensions instead.
        n = len(completeness)
        return {k: 1.0 / n for k in completeness}
    return {k: v / total for k, v in completeness.items()}


def rank_listings(screened_df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes the output of phase1_rules.run_screener() (already has
    Screening_Status / Screening_Reasons / Flag_Count columns), filters to
    Phase 1 survivors exactly the way costar's original ranking.py main()
    did (Screening_Status != "ELIMINATED" -- this KEEPS "FLAGGED" rows,
    only "ELIMINATED" rows are dropped), scores the survivors across the
    5 dimensions, and returns them sorted by Composite_Score descending.
    """
    survivors = screened_df[screened_df["Screening_Status"] != "ELIMINATED"].copy()

    weights = compute_dynamic_weights(survivors)

    survivors["Score_DaysOnMarket"] = percentile_score(survivors["Days On Market"], higher_is_better=False)
    survivors["Score_Price"] = percentile_score(survivors["For Sale Price"], higher_is_better=False)
    survivors["Score_LandUseCategory"] = score_land_use_category(survivors)
    survivors["Score_DevelopedEnv"] = score_developed_environment(survivors)
    survivors["Score_FloodRisk"] = score_flood_risk(survivors)

    survivors["Composite_Score"] = (
        survivors["Score_DaysOnMarket"] * weights["days_on_market"]
        + survivors["Score_Price"] * weights["price"]
        + survivors["Score_LandUseCategory"] * weights["land_use_category"]
        + survivors["Score_DevelopedEnv"] * weights["developed_environment"]
        + survivors["Score_FloodRisk"] * weights["flood_risk"]
    ).round(1)

    survivors = survivors.sort_values("Composite_Score", ascending=False)
    return survivors


if __name__ == "__main__":
    # Thin manual-test CLI wrapper -- the real entry point is rank_listings()
    # called from analysis/screening/pipeline.py.
    import sys
    from . import phase1_rules

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python -m analysis.screening.phase2_ranking <path_to_costar_export.xlsx>")
        sys.exit(1)
    df = pd.read_excel(path)
    scored = phase1_rules.run_screener(df)
    ranked = rank_listings(scored)
    print(ranked[["Composite_Score", "Property Address"]].head(10).to_string())
