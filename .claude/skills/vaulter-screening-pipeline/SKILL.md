---
name: vaulter-screening-pipeline
description: Use when a new CoStar/broker export needs to go from raw file to a trustworthy, shareable result — screen it, verify the screener read it fairly, map proximity on the shortlist, and confirm the report is correct and readable before handing it to a partner. Invoke when screening something "for real" / "to send out", not for a quick exploratory look.
argument-hint: <filename-or-substring> [moic-target]
---

# Screening pipeline — orchestrator

You are the orchestrator. This skill exists so the same checks run every time, instead of an ad
hoc "does this look okay?" pass that catches something different each time. It wraps
`screening-run` with the two QA gates that answer the actual complaint this was built for:
reviewing the screener kept turning up one more issue, then another, with no end in sight.

## Steps

1. **Run the screen.** Follow `screening-run` — resolve the file, run `main.py screen` (or
   `screen_listings`), get the ranked output and `column_sources`.
2. **Screening QA gate — mandatory, every time.** Delegate to `vaulter-screening-qa` with the
   file path. Do not skip this because the file "looks like a normal export" — every past bug
   looked normal until it didn't.
   - If it returns **"Do not trust until: X"** — fix X, re-run the screen, and re-run the QA
     agent. Don't hand-wave past a FAIL.
   - Only proceed once it returns **"Safe to trust."**
3. **Proximity on the shortlist.** For the top candidates (not the whole file), call
   `run_proximity_for_listing` for each, and `compare_proximity_to_portfolio` where a nearby
   holding exists. This is a direct tool call, not a subagent — one call per candidate, no
   isolation benefit from delegating it.
4. **Ground truth.** Run `verify_listings` on the same shortlist — FEMA flood over the parcel
   area, road access, incorporated-place status.
5. **Build the report.** Run `report.py` (or however `open_screening_dashboard` triggers it) to
   produce the HTML.
6. **Dashboard QA gate — mandatory before sharing.** Delegate to `vaulter-dashboard-qa` against
   the generated report. Fix anything it flags and rebuild before calling this done.

## What this is not

- **Not automatic.** You trigger this yourself, in conversation, when you want a result solid
  enough to act on or share — not for a quick "what's roughly in this file" glance, where
  `screening-run` alone is enough.
- **Not a replacement for your own judgment** on the shortlist (nearest holding, entitlement
  risk, hold-period realism) — the QA gates check that the *tool* was fair and correct, not that
  the *deal* is good.

## Reporting back

State plainly which gate(s) caught something and what was fixed, not just a final "all clear" —
that's the record that stops the same bug from being rediscovered on the next file. If a gate
found nothing, say that too, briefly.
