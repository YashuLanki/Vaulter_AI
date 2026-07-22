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
import os
import time
from datetime import datetime
from pathlib import Path

import openpyxl
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


def _merge_manifest_conflict(official: dict, conflict: dict) -> dict:
    """Folds a conflict copy's market entries into the official manifest.
    A plain union is safe here specifically because entries are uniquely
    keyed by (market_slug, source_hash, top_n, include_low_value_apis) --
    the same key _update_manifest's own eviction logic uses -- so an
    entry already present in `official` is never duplicated or
    overwritten by the conflict copy's version of it."""
    official.setdefault("markets", [])
    existing_keys = {
        (m.get("market_slug"), m.get("source_hash"), m.get("top_n"), m.get("include_low_value_apis", False))
        for m in official["markets"]
    }
    for m in conflict.get("markets", []):
        key = (m.get("market_slug"), m.get("source_hash"), m.get("top_n"), m.get("include_low_value_apis", False))
        if key not in existing_keys:
            official["markets"].append(m)
            existing_keys.add(key)
    official["markets"].sort(key=lambda m: m.get("timestamp", ""), reverse=True)
    return official


def _merge_flat_cache_conflict(official: dict, conflict: dict) -> dict:
    """For the flat key->value shared caches (Phase 3 listing analyses,
    Phase 4 verdicts, market geocodes) -- add any entry the conflict copy
    has that the official file doesn't. `official` wins if both sides
    somehow recomputed the exact same key, since it reflects whatever was
    already confirmed on disk most recently."""
    return {**conflict, **official}


def _reconcile_shared_files(output_dir: Path) -> None:
    """
    Finds and merges any OneDrive conflict copies of this project's 4
    shared screening files back into their official versions before this
    run reads any of them -- maximizing the chance of a cache hit and
    recovering entries that would otherwise sit invisible in a renamed
    conflict copy forever (see C2 in docs/MULTI_USER_TRANSITION.md).
    Best-effort: any failure here is logged and never blocks screening
    itself, since this is a reconciliation nicety, not a required step.
    """
    try:
        safe_io.merge_conflict_copies(output_dir / "manifest.json", _merge_manifest_conflict)
        for filename in ("phase3_listing_cache.json", "phase4_verdict_cache.json", "market_geocode_cache.json"):
            safe_io.merge_conflict_copies(output_dir / filename, _merge_flat_cache_conflict)
    except Exception as e:
        log.warning(f"Could not reconcile OneDrive conflict copies of shared screening files: {e}")


# How long an in-progress marker is trusted as "someone is still actively
# working on this" before being treated as abandoned (a crashed run, or
# one that never got a chance to clean up after itself). Also doubles as
# the longest a second caller will wait for the first caller's result
# before giving up and running the screen itself -- generous enough to
# cover a realistic Phase 3 (up to 15 Claude calls) + Phase 4 (up to 10
# finalists' worth of Google Maps + Claude calls) run, short enough that
# an abandoned marker doesn't block this file for the rest of the day.
IN_PROGRESS_MARKER_TTL_SECONDS = 15 * 60
IN_PROGRESS_POLL_INTERVAL_SECONDS = 10


def _marker_path(output_dir: Path, source_hash: str, top_n: int, include_low_value_apis: bool) -> Path:
    return output_dir / f"in_progress_{source_hash}_{top_n}_{int(include_low_value_apis)}.json"


def _marker_is_fresh(marker_path: Path) -> bool:
    """A marker only counts as "someone is actively working on this" if
    it's both present and recent. An old one almost certainly means
    whoever created it crashed, was interrupted, or the process was
    killed before it could clean up after itself -- not that they're
    still genuinely working on it 15+ minutes later."""
    data = safe_io.load_json(marker_path)
    if not data:
        return False
    started_at = data.get("started_at")
    if not started_at:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return False
    return age < IN_PROGRESS_MARKER_TTL_SECONDS


def _claim_in_progress(marker_path: Path) -> None:
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    safe_io.save_json_atomic(marker_path, {"started_at": datetime.now().isoformat(), "pid": os.getpid()})


def _release_in_progress(marker_path: Path) -> None:
    """Best-effort cleanup -- if this fails, the marker just sits there
    until it goes stale (see IN_PROGRESS_MARKER_TTL_SECONDS) and gets
    treated as abandoned by the next caller; it never blocks this file
    forever."""
    try:
        marker_path.unlink(missing_ok=True)
    except OSError as e:
        log.warning(f"Could not remove in-progress marker {marker_path}: {e}")


WORKBOOK_VALIDATION_RETRY_ATTEMPTS = 3
WORKBOOK_VALIDATION_RETRY_DELAY_SECONDS = 0.5


def _workbook_is_valid(workbook_path: Path) -> bool:
    """
    Confirms a cached result's workbook file is actually fully present
    and openable -- not just that a filesystem entry for it exists.

    OneDrive can finish syncing a small manifest.json update to this
    machine before its much larger paired workbook.xlsx has finished
    downloading -- so path.exists() alone is not proof the file is
    actually usable yet; it can be a placeholder or a partial download
    that reads as corrupt. Retries briefly to ride out a file still
    mid-sync before giving up (see C4 in docs/MULTI_USER_TRANSITION.md).
    """
    for attempt in range(WORKBOOK_VALIDATION_RETRY_ATTEMPTS):
        try:
            wb = openpyxl.load_workbook(workbook_path, read_only=True)
            wb.close()
            return True
        except Exception:
            if attempt < WORKBOOK_VALIDATION_RETRY_ATTEMPTS - 1:
                time.sleep(WORKBOOK_VALIDATION_RETRY_DELAY_SECONDS)
    return False


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
    if found and its workbook file is actually present and openable, else
    None.
    """
    manifest = _load_manifest(output_dir)
    for entry in manifest.get("markets", []):
        if (entry.get("source_hash") != source_hash
                or entry.get("top_n") != top_n
                or entry.get("include_low_value_apis", False) != include_low_value_apis):
            continue
        workbook_path = output_dir / entry.get("workbook", "")
        if not workbook_path.exists() or not _workbook_is_valid(workbook_path):
            # Recorded but the file is gone, still mid-sync, or corrupt --
            # don't serve a dangling/incomplete reference (see C4 in
            # docs/MULTI_USER_TRANSITION.md). Falls through to a fresh
            # screening run rather than risking handing back a broken file.
            continue
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

    Before any of that, reconciles any OneDrive conflict copies of the 4
    shared screening files back into their official versions (see C2 in
    docs/MULTI_USER_TRANSITION.md) -- this maximizes the chance step 0's
    cache lookup actually finds a teammate's already-paid-for result.

    If someone else's screening run for this EXACT file/settings is
    already in progress (a fresh in-progress marker exists), waits for
    their result instead of independently re-running Phase 3/4 and paying
    for the same Claude/Google Maps calls a second time -- see C3 in
    docs/MULTI_USER_TRANSITION.md.
    """
    output_dir = screening_config.SCREENING_OUTPUT_DIR
    _reconcile_shared_files(output_dir)
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

    marker_path = _marker_path(output_dir, source_hash, top_n, include_low_value_apis)
    if _marker_is_fresh(marker_path):
        log.info(f"Another in-progress screening run found for this exact file/settings -- "
                  f"waiting up to {IN_PROGRESS_MARKER_TTL_SECONDS // 60} min for it to finish "
                  f"instead of re-running Phase 3/4 a second time.")
        waited = 0
        while waited < IN_PROGRESS_MARKER_TTL_SECONDS:
            time.sleep(IN_PROGRESS_POLL_INTERVAL_SECONDS)
            waited += IN_PROGRESS_POLL_INTERVAL_SECONDS
            cached = _find_cached_result(output_dir, source_hash, top_n, include_low_value_apis)
            if cached:
                log.info("The other in-progress run finished while we waited -- reusing its result.")
                return cached
            if not _marker_is_fresh(marker_path):
                log.info("The other run's marker went stale before finishing (likely crashed or "
                          "interrupted) -- proceeding with our own run instead of waiting further.")
                break
        else:
            log.info("Gave up waiting for the other in-progress run to finish -- proceeding with our own.")

    _claim_in_progress(marker_path)
    try:
        return _execute_screening_phases(
            source_bytes, source_hash, top_n, include_low_value_apis,
            anthropic_api_key, google_api_key, output_dir,
        )
    finally:
        _release_in_progress(marker_path)


def _execute_screening_phases(
    source_bytes: bytes,
    source_hash: str,
    top_n: int,
    include_low_value_apis: bool,
    anthropic_api_key: str,
    google_api_key: str | None,
    output_dir: Path,
) -> dict:
    """
    Runs Phases 1-4, builds the combined workbook, and updates the shared
    manifest. Split out from run_full_screening() specifically so the
    caller can wrap just this expensive part in a try/finally that always
    releases the in-progress marker (see C3 in docs/MULTI_USER_TRANSITION.md)
    -- the cache check and marker wait/claim logic above this needs to run
    BEFORE the marker is claimed, not inside this function's own scope.
    """
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

    # source_hash[:8] guarantees two DIFFERENT files screened for the SAME
    # market within the same second (timestamp has only 1-second
    # resolution) still get distinct filenames -- two people screening the
    # exact SAME file/settings never reach here at all, since that's
    # already served from cache above (see C5 in
    # docs/MULTI_USER_TRANSITION.md).
    workbook_filename = f"{screening_config.COMBINED_WORKBOOK_PREFIX}_{market_slug}_{timestamp}_{source_hash[:8]}.xlsx"
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

    try:
        _update_manifest(output_dir, market, market_slug, timestamp, workbook_filename,
                          source_hash, top_n, include_low_value_apis, summary)
    except safe_io.UnreadableFileError as e:
        # Phase 3/4 (the expensive, already-paid-for part) already
        # succeeded and the workbook is already saved at workbook_path --
        # don't throw all of that away just because the shared manifest
        # couldn't be updated this instant. The only cost of proceeding
        # here is that this result won't be served from cache to
        # teammates until a future run's manifest update succeeds; the
        # workbook itself is not lost or orphaned; retrying with the same
        # file will simply write a fresh manifest entry next time.
        log.error(f"Screening completed and the workbook was saved to {workbook_path}, but "
                  f"the shared team manifest could not be updated: {e} This run's result "
                  f"won't be reused from cache by teammates until a future manifest update "
                  f"succeeds.")

    return {**summary, "cached": False}
