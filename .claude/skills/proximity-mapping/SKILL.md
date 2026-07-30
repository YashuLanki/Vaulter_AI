---
name: proximity-mapping
description: Use when the user wants proximity/POI analysis around a portfolio property or a screened listing — what's nearby, how a candidate compares to what the firm already owns, or a batch of proximity runs across a shortlist. This is the proximity desk's lead playbook; it dispatches worker subagents when a result needs checking, and knows which failure modes to distrust.
argument-hint: <property-name | listing-rank> [radius]
---

# Proximity desk — lead playbook

You are the orchestrator for everything proximity. The deterministic core is
`pipeline/proximity_tool.py` (one Overpass query, all POI categories at once, classified
locally, CSV output to the shared folder). Your job is running it correctly, distrusting the
right things, and dispatching workers when a result matters enough to verify.

## Routing — which tool for which target

- **A portfolio property by name** → `run_proximity_for_property`. It looks up the hand-verified
  `property_coordinates.csv` and **refuses if the property has no verified coordinate. Respect
  the refusal** — never work around it by geocoding the name (measured: 5 wrong of 8, two in the
  wrong country, silent failure). If a property is missing a coordinate, that's a finding to
  report, not an obstacle to hack past.
- **A screened listing** → `run_proximity_for_listing` with its rank from the most recent screen.
  Uses the CoStar export's own coordinates, so the refusal rule doesn't apply.
- **Candidate vs. what the firm owns** → `compare_proximity_to_portfolio` after the listing run.
  Both entry points produce the same format deliberately, so the comparison is direct.
- **A whole shortlist** → run listings one at a time (each is one Overpass query). Don't
  parallelize into the same mirror — that's how rate-limiting starts.

## What to distrust — the desk's own history

1. **A fast empty answer is the most dangerous answer.** A Switzerland-only Overpass mirror once
   answered "0 results" for a Phoenix listing — confidently, validly, instantly. Mirrors are now
   coverage-probed and quarantined, but the standing rule survives any code change: **an empty or
   uniform result across rows is a broken query until proven otherwise.** If everything comes
   back empty, check `geo_providers.py`'s mirror state before believing it.
2. **"Provider unreachable" ≠ "nothing there."** The prompt formatter renders these differently
   on purpose. Never summarize an unreachable provider as an empty area.
3. **Output goes to the shared folder, one file per entity, overwritten per run** (CSV only —
   the .xlsx export was removed 2026-07-30). If you see timestamped filenames accumulating
   again, that's a regression of a fixed bug — flag it.

## When to dispatch workers

- **After a large batch** (a full shortlist, or anything that wrote 10+ files) →
  `vaulter-onedrive-auditor` to audit `proximity_output/` for accumulation regressions.
- **Before a proximity finding goes into an investment memo** (e.g. "no rail within 5 miles",
  "nearest school is X") → `vaulter-fact-checker` on that specific claim. One agent per claim,
  parallel is fine.
- **A tool call errors or hangs** → `vaulter-connection-doctor`, per the connector desk's standing rule.

## Reporting back

Lead with what's actually near the property in plain terms, then the comparison to the nearest
holding if one was run. Note explicitly which mirror answered and whether any category came back
empty — silence about an empty category is how the broken-query failure mode gets past a reader.
