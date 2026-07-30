---
name: screening-run
description: Use when asked to screen a CoStar export or broker spreadsheet, rank inbound land listings, decide what to pursue, or when the user invokes /screening-run <file> [moic]
argument-hint: <filename-or-substring> [moic-target]
---

# Screen a CoStar export

Rank listings by **fit against Vaulter's existing portfolio**. Free, instant, no API calls,
works on any market.

## Run it

```powershell
.venv\Scripts\python.exe main.py screen <FILENAME> [MOIC]
```

`<FILENAME>` resolves against `data/drop/` if it isn't a full path. `MOIC` defaults to 3.0;
the firm targets 2.5–3x on predevelopment value-add, so 2.5 is the other sensible value.

Or call the `screen_listings` MCP tool, which does the same thing and also accepts a
base64-pasted file.

## Locating the file

`data/drop/` first, then the document library via `corpus.search`, then the pre-rebuild
`data/watched_folder/` and `data/processed/` trees if they still exist. If the exact name
doesn't resolve, try a shorter substring (e.g. `Costar`) before giving up. `open_costar_folder`
opens the drop folder for the user.

## What it does and doesn't do

- **Eliminates nothing.** Everything is ranked with a stated reason. Low-fit listings sink;
  they don't disappear. Do not reintroduce hard filters — see `fit_screen.py`'s docstring for
  the measured damage they did.
- **Scores** proximity to existing holdings (heaviest), size-in-context, MOIC-based pricing,
  and distress-as-upside.
- **Costs nothing.** No Claude API call, no geo API call, no per-listing cost.

## After it returns — this is the important half

The tool gives you the ranked shortlist and the arithmetic. **You** do the judgment, in the
conversation, for free:

- Read the top candidates and say which are genuinely worth pursuing and why.
- Cross-reference the document library — `search_documents` / `read_document` — for the
  nearest holding named in each row. A listing 1.6mi from an owned property should be read
  against what that property's files actually say about the submarket, its utilities, and
  its politics.
- Weigh entitlement risk, net-vs-gross acreage, and hold-period realism.

The 4-phase pipeline is gone — `pipeline.py`, `phase1_rules.py`, `phase2_ranking.py`,
`phase3_deep_analysis.py`, `phase4_verification.py` and their helpers were deleted once nothing
reached them. Don't look for them, and don't propose reviving them.

Ground truth runs through `verify_listings` — free and keyless: FEMA flood zones over the parcel
**area**, Census TIGER road access, incorporated-place status, terrain.

`run_proximity_for_listing <rank>` maps everything within a radius of a candidate — employers,
retail, schools, utilities, nuisance. The screen says how close a listing is to land the firm
owns; this says what is actually there. Run it on a shortlist candidate, not on the whole file.

## Reporting back

- Tier counts, then the top candidates with the one-line "why" already generated.
- **Always surface the time reality**: the firm publishes 2.40x @5yr / 1.71x @10yr /
  1.61x @15yr, and documented holds ran 12–16 years against 36–48 months underwritten. A 3x
  at 4 years is 31.6% IRR; the same 3x at 14 years is 8.2%.
- **Always note the assumptions aren't ratified.** `docs/COMPANY_PROFILE.md` is a draft
  derived from documents and confirmed by nobody; the weights and the 35% carry load are
  guesses in `fit_screen.ASSUMPTIONS`. Say so rather than presenting output as authoritative.

## Common mistakes

| Mistake | Instead |
|---|---|
| Looking for `run_full_screening` or a phase module | They're deleted. `main.py screen` or `screen_listings`. |
| Treating a low Fit_Score as "rejected" | It's a ranking, not a filter. Say why it ranked low. |
| Quoting a 3x MOIC without the hold period | Give the IRR at both 4yr and 14yr |
| Presenting scores as the firm's ratified standard | Flag the draft status every time |
| Bare `python` | `.venv\Scripts\python.exe` (deps live in the venv) |
