"""
analysis/screening/market_utils.py
-------------------------------------
Small shared helpers, pulled out of costar_screener's run_pipeline.py.

detect_market() always reads the CoStar export's own "Market Name" column
-- never hardcoded -- so the pipeline works correctly against any market's
export without manual editing.
"""

import logging
import re

import pandas as pd

log = logging.getLogger("vaulter.screening")


def detect_market(df: pd.DataFrame) -> str:
    """Reads the CoStar export's own Market Name column -- same dynamic
    approach used throughout this project, never hardcoded."""
    if "Market Name" not in df.columns:
        return "Unknown Market"
    values = df["Market Name"].dropna().unique()
    if len(values) == 0:
        return "Unknown Market"
    if len(values) > 1:
        log.warning(f"CoStar export contains multiple Market Name values: {list(values)}. "
                    f"Using the first one for tagging: {values[0]}")
    return str(values[0])


def slugify(market_name: str) -> str:
    raw = "".join(c if c.isalnum() else "_" for c in market_name)
    return re.sub(r"_+", "_", raw).strip("_")
