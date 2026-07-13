"""
analysis/screening/config.py
-----------------------------
Vaulter CoStar Listing Screener — Configuration (shared by Phase 1 and Phase 2)

Single source of truth for all rules and settings. phase1_rules.py and
phase2_ranking.py read from this file -- nothing is hardcoded elsewhere.

Rule types:
  - "threshold"        : compares column to a fixed value using `operator`
  - "missing_or_lte"    : triggers if column is null OR <= `value`
  - "not_in_list"       : triggers if column value is NOT in `value` (a list)
  - "dynamic_iqr_upper" : triggers if column exceeds Q3 + 1.5*IQR,
                          recalculated fresh from the loaded dataset each run

Actions:
  - "eliminate" : listing is removed immediately
  - "flag"      : listing is kept but marked for manual review

COMPOUND_RULES apply on top of individual rules (e.g. "too many flags").

Output location:
  This merged project reuses the root-level config.py's DATA_DIR convention
  (see /config.py — same pattern as PROCESSED_DIR, RAW_WEB_DIR, etc.) instead
  of costar_screener's standalone "data/output" path. All screening outputs
  (workbooks + manifest.json) live under DATA_DIR / "screening_output", and
  uploaded/pasted CoStar files live under DATA_DIR / "screening_uploads".
"""

from config import DATA_DIR, SCREENING_OUTPUT_DIR

# Kept for reference/back-compat with costar_screener's original naming.
# OUTPUT_DIR now points at the merged project's own data dir rather than
# costar_screener's standalone "data/output".
OUTPUT_DIR = SCREENING_OUTPUT_DIR
INPUT_FILE = "CostarExport.xlsx"  # default filename, resolved dynamically by the MCP tool

# Phase 1 output filenames: {OUTPUT_DIR}/{SCREENING_OUTPUT_PREFIX}_<timestamp>.xlsx
SCREENING_OUTPUT_PREFIX = "screening_results"

# Phase 2 output filenames: {OUTPUT_DIR}/{RANKING_OUTPUT_PREFIX}_<timestamp>.xlsx
RANKING_OUTPUT_PREFIX = "ranked_results"

# Combined-workbook output filenames: {OUTPUT_DIR}/screening_<market_slug>_<timestamp>.xlsx
COMBINED_WORKBOOK_PREFIX = "screening"

# Columns to include in output, in addition to Screening_Status,
# Screening_Reasons, and Flag_Count (always added automatically).
OUTPUT_COLUMNS = [
    "Property Address",
    "Property Name",
    "City",
    "State",
    "Market Name",
    "Submarket Name",
    "Land Area (AC)",
    "For Sale Price",
    "Secondary Type",
    "Proposed Land Use",
    "Flood Risk Area",
    "Days On Market",
    "Number of Stories",
]

RULES = [
    {
        "id": "min_acreage",
        "column": "Land Area (AC)",
        "type": "threshold",
        "operator": "<=",
        "value": 5,
        "action": "eliminate",
        "reason": "Below 5 acres — under Vaulter's minimum viable value-add scale",
    },
    {
        "id": "high_flood_risk",
        "column": "Flood Risk Area",
        "type": "threshold",
        "operator": "==",
        "value": "High Risk Areas",
        "action": "eliminate",
        "reason": "High flood risk area — materially increases entitlement cost/timeline",
    },
    {
        "id": "missing_price",
        "column": "For Sale Price",
        "type": "missing_or_lte",
        "value": 0,
        "action": "flag",
        "reason": "Missing or zero listed price — may be 'call for pricing' or incomplete record",
    },
    {
        "id": "undetermined_flood_risk",
        "column": "Flood Risk Area",
        "type": "threshold",
        "operator": "==",
        "value": "Undetermined Risk Areas",
        "action": "flag",
        "reason": "Flood risk undetermined — needs manual verification",
    },
    {
        "id": "stale_listing",
        "column": "Days On Market",
        "type": "dynamic_iqr_upper",
        "action": "flag",
        "reason": "Days on market is a statistical outlier (beyond 1.5x IQR above Q3) — worth checking why it hasn't moved",
    },
    {
        "id": "high_price",
        "column": "For Sale Price",
        "type": "threshold",
        "operator": ">",
        "value": 8_000_000,
        "action": "flag",
        "reason": "Listed price exceeds $8,000,000",
    },
    {
        "id": "land_use_category",
        "column": "Secondary Type",
        "type": "not_in_list",
        "value": ["Residential", "Commercial", "Industrial", "Mixed Use"],
        "action": "flag",
        "reason": "Secondary Type outside Vaulter's target categories (Residential, Commercial, Industrial, Mixed Use)",
    },
    {
        "id": "existing_structure",
        "column": "Number of Stories",
        "type": "not_missing",
        "action": "eliminate",
        "reason": "Number of Stories is populated despite being classified as raw Land — likely has an existing structure on site, not true vacant land",
    },
]

COMPOUND_RULES = [
    {
        "id": "two_plus_flags",
        "min_flags": 2,
        "action": "eliminate",
        "reason": "Hit 2 or more flag conditions — cumulative risk too high",
    },
]
