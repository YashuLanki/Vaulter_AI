"""
analysis/screening/phase1_rules.py
------------------------------------
Vaulter CoStar Listing Screener — Phase 1 Rule Engine

The rule-application logic, kept separate so both the pipeline orchestrator
and Phase 2's ranking module can call it. Contains NO hardcoded column names
or thresholds -- every rule is read generically from analysis/screening/config.py.
"""

import logging

import pandas as pd

from . import config

log = logging.getLogger("vaulter.screening")


def compute_dynamic_threshold(series: pd.Series) -> float:
    """Q3 + 1.5*IQR, recalculated fresh from whatever data is loaded."""
    q1 = series.quantile(0.25)
    q3 = series.quantile(0.75)
    iqr = q3 - q1
    return q3 + 1.5 * iqr


def evaluate_rule(df: pd.DataFrame, rule: dict) -> pd.Series:
    """Returns a boolean Series: True where this rule triggers."""
    col = df[rule["column"]]
    rtype = rule["type"]

    if rtype == "threshold":
        op = rule["operator"]
        val = rule["value"]
        if op == "<=":
            return col <= val
        elif op == ">=":
            return col >= val
        elif op == "<":
            return col < val
        elif op == ">":
            return col > val
        elif op == "==":
            return col == val
        elif op == "!=":
            return col != val
        else:
            raise ValueError(f"Unknown operator: {op}")

    elif rtype == "missing_or_lte":
        return col.isna() | (col <= rule["value"])

    elif rtype == "not_in_list":
        return ~col.isin(rule["value"])

    elif rtype == "not_missing":
        return col.notna()

    elif rtype == "dynamic_iqr_upper":
        threshold = compute_dynamic_threshold(col.dropna())
        log.info(f"  [{rule['id']}] dynamic threshold = {threshold:.1f}")
        return col > threshold

    else:
        raise ValueError(f"Unknown rule type: {rtype}")


def run_screener(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    eliminate_reasons = [[] for _ in range(len(df))]
    flag_reasons = [[] for _ in range(len(df))]

    log.info("Applying rules:")
    for rule in config.RULES:
        triggered = evaluate_rule(df, rule)
        count = int(triggered.sum())
        log.info(f"  [{rule['id']}] triggered on {count} listings -> {rule['action']}")

        for idx in df.index[triggered]:
            pos = df.index.get_loc(idx)
            if rule["action"] == "eliminate":
                eliminate_reasons[pos].append(rule["reason"])
            elif rule["action"] == "flag":
                flag_reasons[pos].append(rule["reason"])

    for crule in config.COMPOUND_RULES:
        for pos in range(len(df)):
            if len(eliminate_reasons[pos]) == 0 and len(flag_reasons[pos]) >= crule["min_flags"]:
                eliminate_reasons[pos].append(
                    f"{crule['reason']} ({len(flag_reasons[pos])} flags: "
                    + "; ".join(flag_reasons[pos]) + ")"
                )

    status = []
    reasons_out = []
    flag_count_out = []
    for pos in range(len(df)):
        if eliminate_reasons[pos]:
            status.append("ELIMINATED")
            reasons_out.append(" | ".join(eliminate_reasons[pos]))
        elif flag_reasons[pos]:
            status.append("FLAGGED")
            reasons_out.append(" | ".join(flag_reasons[pos]))
        else:
            status.append("PASS")
            reasons_out.append("")
        flag_count_out.append(len(flag_reasons[pos]))

    df.insert(0, "Screening_Status", status)
    df.insert(1, "Screening_Reasons", reasons_out)
    df.insert(2, "Flag_Count", flag_count_out)
    return df


def get_display_df(scored_df: pd.DataFrame) -> pd.DataFrame:
    """Trims to just Screening columns + config.OUTPUT_COLUMNS."""
    display_cols = ["Screening_Status", "Screening_Reasons", "Flag_Count"] + [
        c for c in config.OUTPUT_COLUMNS if c in scored_df.columns
    ]
    return scored_df[display_cols]


if __name__ == "__main__":
    # Thin manual-test CLI wrapper -- the real entry point is run_screener()
    # called from analysis/screening/pipeline.py.
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("Usage: python -m analysis.screening.phase1_rules <path_to_costar_export.xlsx>")
        sys.exit(1)
    df = pd.read_excel(path)
    scored = run_screener(df)
    print(scored["Screening_Status"].value_counts())
