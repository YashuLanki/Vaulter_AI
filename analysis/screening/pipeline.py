"""
analysis/screening/pipeline.py
---------------------------------
Single entry point for the full 4-phase CoStar listing screening pipeline.
Callable directly from an MCP tool handler (see mcp_server.py's
screen_listings tool) -- no subprocess calls, no re-running of upstream
phases, no CLI orchestration.
"""

import hashlib
import io
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

import safe_io
from . import config as screening_config
from . import phase1_rules
from . import phase2_ranking
from . import phase3_deep_analysis
from . import phase4_verification
from . import market_utils
from . import workbook_builder

log = logging.getLogger("vaulter.screening")


def _load_manifest(output_dir: Path) -> dict:
    return safe_io.load_json(output_dir / "manifest.json", default={"markets": []})


def _find_cached_result(output_dir: Path, source_hash: str, top_n: int,
                         include_low_value_apis: bool) -> dict | None:
    """
    Looks for a prior screening run of this EXACT file content (by hash) at
    the SAME top_n depth and SAME include_low_value_apis setting (a run
    that skipped the low-value Google APIs shouldn't be served back to
    someone who explicitly asked for the fuller picture, or vice versa).
    SCREENING_OUTPUT_DIR is shared across the whole team (see config.py's
    SHARED_DIR) -- this is what turns "everyone independently re-screens
    the same CoStar file" into "first person pays for it, everyone else's
    Claude reads the same result for free."

    Returns the cached summary dict (same shape run_full_screening returns)
    if found and its workbook file still exists on disk, else None.
    """
    manifest = _load_manifest(output_dir)
    for entry in manifest.get("markets", []):
        if (entry.get("source_hash") != source_hash
                or entry.get("top_n") != top_n
                or entry.get("include_low_value_apis", False) != include_low_value_apis):
            continue
        workbook_path = output_dir / entry.get("workbook", "")
        if not workbook_path.exists():
            continue  # recorded but the file is gone -- don't trust a dangling reference
        return {
            "market": entry.get("market"),
            "workbook_path": str(workbook_path.resolve()),
            "total_screened": entry.get("total_screened"),
            "phase1_survivors": entry.get("phase1_survivors"),
            "top10_addresses": entry.get("top10_addresses", []),
            "finalist_addresses": entry.get("finalist_addresses", []),
            "finalist_tiers": entry.get("finalist_tiers", {}),
            "top_candidates": entry.get("top_candidates", []),
            "cached": True,
            "cached_from_timestamp": entry.get("timestamp"),
        }
    return None


def _update_manifest(output_dir: Path, market: str, market_slug: str, timestamp: str,
                      workbook_filename: str, source_hash: str, top_n: int,
                      include_low_value_apis: bool, summary: dict) -> Path:
    """
    Updates the shared manifest.json under an exclusive file lock (see
    safe_io.locked_json_update) -- this file lives in SHARED_DIR and can
    be written by any team member's own instance, so without a lock, two
    people finishing a screening run around the same time could have one
    process's write silently discard the other's just-added entry (the
    write here happens AFTER Phase 3/4 complete, so losing it would mean
    redoing all of that work's caching benefit for nothing).
    """
    manifest_path = output_dir / "manifest.json"
    new_entry = {
        "market": market,
        "market_slug": market_slug,
        "timestamp": timestamp,
        "workbook": workbook_filename,
        "source_hash": source_hash,
        "top_n": top_n,
        "include_low_value_apis": include_low_value_apis,
        "total_screened": summary["total_screened"],
        "phase1_survivors": summary["phase1_survivors"],
        "top10_addresses": summary["top10_addresses"],
        "finalist_addresses": summary["finalist_addresses"],
        "finalist_tiers": summary["finalist_tiers"],
        "top_candidates": summary["top_candidates"],
    }

    def _apply(manifest: dict) -> dict:
        manifest.setdefault("markets", [])
        # Only drop the entry this new one is an exact duplicate of --
        # same market AND same source_hash/top_n/include_low_value_apis,
        # matching _find_cached_result's own lookup key exactly. Evicting
        # by market_slug alone would silently discard every OTHER
        # still-valid cached combo for this same market (a different
        # CoStar export, a different top_n, a different API setting),
        # each with its own workbook file left on disk with nothing in
        # the manifest pointing to it anymore -- an orphan that
        # _find_cached_result can then never serve back to anyone, even
        # for the exact file/settings that produced it.
        manifest["markets"] = [
            m for m in manifest["markets"]
            if not (m.get("market_slug") == market_slug
                    and m.get("source_hash") == source_hash
                    and m.get("top_n") == top_n
                    and m.get("include_low_value_apis", False) == include_low_value_apis)
        ]
        manifest["markets"].append(new_entry)
        manifest["markets"].sort(key=lambda m: m["timestamp"], reverse=True)
        return manifest

    safe_io.locked_json_update(manifest_path, _apply, default={"markets": []})
    return manifest_path


def run_full_screening(
    source_path: Path,
    anthropic_api_key: str,
    google_api_key: str | None,
    top_n: int = phase3_deep_analysis.TOP_N_DEFAULT,
    include_low_value_apis: bool = False,
) -> dict:
    """
    Runs the full 4-phase screening pipeline against source_path (a CoStar
    export or broker spreadsheet) and returns a summary dict suitable for
    turning into a text reply for Claude.

    Steps:
      0. Check the shared manifest for an already-screened result for this
         exact file content (by hash) at this same top_n -- if found, return
         it directly without re-running anything, since SCREENING_OUTPUT_DIR
         is shared across the whole team and someone may have already paid
         for this exact screen.
      1. Read source_path with pd.read_excel
      2. Phase 1 -- phase1_rules.run_screener
      3. Phase 2 -- phase2_ranking.rank_listings
      4. Detect market via market_utils
      5. Phase 3 -- phase3_deep_analysis.get_top_listings + run_deep_analysis
      6. Phase 4 -- phase4_verification.run_verification
      7. Build the combined workbook via workbook_builder.build_combined_workbook
      8. Update manifest.json
      9. Return a summary dict
    """
    output_dir = screening_config.SCREENING_OUTPUT_DIR
    # Read the file's bytes ONCE, immediately, and reuse them for both the
    # hash and the actual Excel parse below (via BytesIO) instead of
    # re-opening source_path a second time later. A pasted/uploaded CoStar
    # file lands in the ingestion watcher's own drop zone (see
    # mcp_server.py::_resolve_costar_source) and the watcher can move it
    # to processed/ once it notices it -- re-reading the path later would
    # race that move. Reading everything into memory up front means the
    # file only needs to still exist for this one read.
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()

    cached = _find_cached_result(output_dir, source_hash, top_n, include_low_value_apis)
    if cached:
        log.info(f"Reusing existing screening result from {cached['cached_from_timestamp']} "
                  f"(identical file content already screened at top_n={top_n}) -- "
                  f"skipping Phase 3/4, no new API calls made.")
        return cached

    df = pd.read_excel(io.BytesIO(source_bytes))
    total_screened = len(df)

    screened_df = phase1_rules.run_screener(df)
    phase1_survivors = int((screened_df["Screening_Status"] != "ELIMINATED").sum())

    ranked_df = phase2_ranking.rank_listings(screened_df)

    market = market_utils.detect_market(df)
    market_slug = market_utils.slugify(market)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    top_listings = phase3_deep_analysis.get_top_listings(ranked_df, top_n)
    top10_addresses = top_listings["_Screening_Key"].tolist() if "_Screening_Key" in top_listings.columns else []

    deep_analyses = phase3_deep_analysis.run_deep_analysis(
        ranked_df, anthropic_api_key, top_n=top_n, cache_dir=output_dir,
    )

    final_result = phase4_verification.run_verification(
        ranked_df, deep_analyses, anthropic_api_key, google_api_key, top_n=top_n, cache_dir=output_dir,
        include_low_value_apis=include_low_value_apis,
    )
    finalist_addresses = final_result.get("finalists", [])

    finalist_tiers = {}
    for addr in finalist_addresses:
        if addr in deep_analyses:
            finalist_tiers[addr] = phase4_verification.classify_tier(deep_analyses[addr].get("RECOMMENDATION", ""))

    workbook_filename = f"{screening_config.COMBINED_WORKBOOK_PREFIX}_{market_slug}_{timestamp}.xlsx"
    workbook_path = output_dir / workbook_filename

    workbook_builder.build_combined_workbook(
        screened_df, ranked_df, deep_analyses, final_result, workbook_path,
    )

    top_candidates = []
    for _, row in top_listings.head(5).iterrows():
        addr = row.get("_Screening_Key", row.get("Property Address", "Unknown"))
        recommendation = deep_analyses.get(addr, {}).get("RECOMMENDATION", "")
        snippet = recommendation.splitlines()[0] if recommendation else ""
        top_candidates.append({
            "address": addr,
            "composite_score": row.get("Composite_Score"),
            "recommendation_snippet": snippet,
        })

    summary = {
        "market": market,
        "workbook_path": str(workbook_path.resolve()),
        "total_screened": total_screened,
        "phase1_survivors": phase1_survivors,
        "top10_addresses": top10_addresses,
        "finalist_addresses": finalist_addresses,
        "finalist_tiers": finalist_tiers,
        "top_candidates": top_candidates,
    }

    _update_manifest(output_dir, market, market_slug, timestamp, workbook_filename,
                      source_hash, top_n, include_low_value_apis, summary)

    return {**summary, "cached": False}
