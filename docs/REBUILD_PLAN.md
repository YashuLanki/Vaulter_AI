# Vaulter AI — Rebuild Plan

**Date:** 2026-07-27, revised 2026-07-29
**Status:** Sections 1–4 are **built and merged to `main`** — there is no rebuild branch any
more. Section 5 is still plan. Sections 6–7 describe what exists today.

**Legend:** ✅ done · ⚠️ assumption, needs testing · 🔨 to build · 🛑 decided against

**If you are reading this cold, read §0 first, then §3.** §3 is where the system actually
earns its keep, and it was substantially rewritten on 2026-07-28 when the screener's costs
stopped being invented and started being measured.

---

## 0. What changed on 2026-07-27

The first draft of this plan was built on Claude's Microsoft 365/Teams connector: it was going to
supply email, SharePoint document access, and meeting transcripts, and that was the justification
for deleting `ingestion/`, `email_reader.py`, `outlook_auth.py`, and ChromaDB.

**The verdict came back: no connectors.** Not M365, not Teams.

That could have killed the rebuild. It didn't, because of one fact that was already true and
already noted in the old §9:

> **The firm's SharePoint library is already a synced local folder.**
> `C:\Users\<user>\OneDrive - <firm's account>\<firm's SharePoint library name>`

A connector was never the only way to reach those documents — it was one way. OneDrive already
puts all ~493,000 of them on disk. So document access survived the verdict intact, and the
deletions went ahead on their own merits.

What the verdict actually cost:

| Capability | Status |
|---|---|
| Portfolio documents | ✅ **Unaffected** — read directly off the synced library |
| Email | ❌ **Dropped.** Written here as "to be rebuilt later" — since made permanent, see the 2026-07-29 update below. |
| Teams meeting transcripts | ❌ Dropped (was already retired in the first draft) |
| "Sign in once" onboarding | ✅ Better than planned — there is now nothing to sign into |

The email decision deserves its own note. Dropping it removed ~900 lines, the MSAL/OAuth token
support burden, and the per-person-database privacy architecture that everything else was bent
around. Much of the correspondence is *also* archived in the library as `.msg` files inside the
property folders — see §4's open item.

### Update 2026-07-29 — email is not coming back 🛑

"Dropped, to be rebuilt later" is now **dropped, full stop.** The user's words:
*"you can disregard the emails. I will stop them soon."* The inbound flow itself is being
retired at the source, so there is nothing left to ingest and no later rebuild to plan for.

That also closes the `.msg` question that §4 and §7 both left open. `.msg` support was only
ever justified as a cheaper substitute for the email pipeline; with the pipeline gone by
choice rather than by circumstance, adding `extract-msg` would be reaching for correspondence
nobody asked to reach. **Not built, and not pending.** If someone later needs one specific
`.msg`, they can open it in Outlook from the same OneDrive folder `search_documents` already
points them at — the file is right there, `read_document` just declines to parse it.

---

## 1. The thesis

Today this project was a data pipeline (ingest everything into a per-user local database) with a
screening feature attached. Almost every hard problem in `MULTI_USER_TRANSITION.md` — silent
failures, shared-folder concurrency, onboarding friction — was a *consequence* of owning that
pipeline, not of the actual business problem.

What's left is the part that was always the real asset:

> **The firm's underwriting standard, encoded as a readable document, applied consistently, with
> an audit trail.**

---

## 2. What was built ✅

### 2.1 `system/corpus/` — document access, replacing the whole ingest stack

The old path was: copy a document into `system/data/watched_folder/` → extract → chunk → embed →
store vectors in ChromaDB → retrieve via `rag_engine`. That was a local duplicate of documents
the filesystem already had, and it existed to work around small context windows.

The new path is: find the file by name, read it, hand the text to Claude.

| Deleted | Lines | Replaced by |
|---|---|---|
| `ingestion/` (watcher, extractor, chunker, embedder, registry) | 1,538 | `system/corpus/index.py` + `system/corpus/extract.py` (~700) |
| `system/analysis/rag_engine.py` | 388 | — |
| ChromaDB, sentence-transformers, watchdog, numpy | — | SQLite (standard library) |

**Two constraints shaped the design, both discovered by measurement, not assumption:**

**Scope.** `CORPUS_DIR` points at the firm's own SharePoint library, *never* the OneDrive account
root one level up — that root also holds the individual's own `Desktop`, `Documents`, and
`Microsoft Teams Chat Files`. `corpus.resolve_in_corpus()` resolves and re-checks every path, so
`../Documents` and absolute paths elsewhere on disk both fail. This is the privacy boundary that
the old per-user-database architecture used to provide.

**Hydration.** The library is synced as OneDrive Files On-Demand *placeholders*
(`ReparsePoint` / `RecallOnDataAccess`). Filenames and folder structure are local and free;
**file contents are not** — opening one downloads it. So:

- **Search matches names and paths only.** Grepping the corpus would have downloaded all of it.
- **Content is read one file at a time**, on a file someone deliberately picked.

This is less of a downgrade than it sounds, because the library's naming convention is dense:
`!PROPERTIES/ARIZONA/Example Ranch 10/01. Legal/Acquisition/Example Ranch 10 Closing Memo.docx`
carries the property, the phase, the document kind, and often a date. But it **is** a real
limitation and the MCP tool descriptions state it outright, because the dangerous failure is
Claude reporting "the firm has no records on X" when it only means "no filename matched X."

**Measurements that drove decisions:**

| | |
|---|---|
| Files in the library | **493,150** |
| JSON index — first attempt | **122 MB**, 467s to build. Rejected. |
| SQLite index — shipped | 122s to build, sub-second queries |
| Index contents | names, sizes, mtimes. **No file contents, ever.** |

Ranking uses three signals: all terms must match, a filename hit beats a folder hit, and a
whole-phrase hit beats both. The phrase bonus is not cosmetic — without it, `Example Ranch 10`
returned *Example Ranch 80* files first, because `10` matches inside any date like `20260107`.
Adjacent numbered parcels are exactly the case that has to come out right.

### 2.2 `system/portfolio.py` — the Project Master reader

Pulled out of `property_scraper.py`, which mixed reading the portfolio with scraping news
headlines in one 1,024-line module. The reader survives at ~270 lines; the scraping is gone.

Dropped in the move: the PDF/OCR parsing path (~250 lines, including rendered-pixel strikethrough
detection), which existed for scanned Smartsheet exports. The export is a clean CSV now.

**A live bug was fixed here.** When `property_coordinates.csv` was added to
`system/data/project_master/`, `find_project_file()` — which just took `files[0]` — started picking it,
and `get_portfolio_list` / `get_properties_by_stage` had been failing outright with
*"Could not extract any properties from property_coordinates.csv."*

### 2.3 Scraping, email, and the background threads — deleted

| Deleted | Lines | Why |
|---|---|---|
| `system/pipeline/web_scraper.py` + `WEB_SOURCES` | 309 | CBRE/Marcus & Millichap/GlobeSt is national brokerage news. What moves raw land value is on next month's planning commission agenda — see §5. |
| `system/pipeline/property_scraper.py` (scraping half) | ~750 | Google News headlines per property |
| `system/pipeline/email_reader.py` + `outlook_auth.py` | 874 | Email dropped |
| `system/pipeline/property_matcher.py` | 217 | Only ever used by the two above |
| `system/pipeline/scheduler.py` + the MCP scheduler thread | ~330 | Nothing left to schedule |
| `system/pipeline/CLAUDE.md` | 160 | Stale duplicate of the root file, describing modules that no longer existed |
| `.env.example` | 60 | Duplicate of `system/confidentials/.env.template` |

`system/mcp_server.py` now starts **no background threads at all**. The "scheduler thread must never
die" constraint is gone with the thread. The one job worth keeping — the daily check for a
published update — moved into `check_system_health`, which Claude already calls once per
conversation. Same cadence in practice, no thread.

### 2.4 The MCP tool surface

**Removed:** `check_inbox_now`, `get_email_highlights` (no email), `get_market_intelligence`
(scraped content is gone), `get_risk_scan` (was a RAG query over chunks that no longer exist),
`get_database_stats` (no database), `open_general_files` (pointed into the deleted tree).

**Added:** `search_documents`, `read_document`, `browse_documents`, `open_costar_folder`.

**Rewired:** `get_property_info` → Project Master record + the property's documents.
`open_property_files` → the real `!PROPERTIES/<STATE>/<Property>/` folder.
`check_system_health` → reports library sync + index age instead of Outlook + ChromaDB + scheduler.

That left 18 tools, down from 19, and every one of them backed by something that exists.

**It is 21 now** (2026-07-29). What arrived after the rebuild, in order:
`apply_pending_update`, `apply_pending_settings` and `get_pending_setup_details` (the
auto-update and org-settings path — §4 of `MULTI_USER_TRANSITION.md`'s Priority 4);
`verify_listings` (federal ground truth on the top-ranked listings, the surviving half of the
old Phase 4); `run_proximity_for_listing` and `compare_proximity_to_portfolio` (proximity on a
*candidate* rather than only on land the firm already owns). `open_general_files` did not come
back. The authoritative list is whatever carries `@mcp.tool()` in `system/mcp_server.py` — count it
there rather than trusting a number in a document, this one included.

### 2.5 Zero API keys required

`GOOGLE_MAPS_API_KEY`, `GOOGLE_PLACES_API_KEY`, `ANTHROPIC_API_KEY` and all three `OUTLOOK_*`
values are gone from `system/config.py` and from `system/confidentials/.env.template`. What replaced them:
ground truth is federal open data (FEMA flood, Census TIGER, USGS elevation, NAIP imagery),
proximity is OpenStreetMap/Overpass, ranking is arithmetic, and the qualitative read happens
in the Claude conversation that asked for it — already paid for.

**No keys at all remain.** A completely blank `system/confidentials/.env` is a working setup; the
file only exists for machines where OneDrive put a folder somewhere unexpected, and for the
one machine that tracks the `canary` release channel. `system/config.py` keeps a short comment block
naming each removed key and what replaced it, so the next person to reach for one finds the
free equivalent before they find a billing page.

Onboarding a new teammate is now: run the wizard, let it index. Nothing to sign into.

---

## 3. Screening — rebuilt 2026-07-28 ✅

`system/analysis/screening/fit_screen.py` replaced Phases 1–2 as the live screener. It ranks by
**fit against the existing portfolio** instead of against absolute thresholds, costs nothing,
and eliminates nothing.

**Why it changed.** The 4-phase pipeline predates `COMPANY_PROFILE.md`. Measured on a real
216-row Arizona export, Phase 1 eliminated 69 listings and **60 of those 69 died on grounds
§5 of the profile explicitly calls not-dealbreakers** — 46 flood, 14 existing structure. One
acquired parcel had roughly 12% of its acreage in floodplain and was bought anyway; another had
a golf course, homes and a cell tower and the firm still offered. Eleven of the eliminations
sat within 3 miles of a current holding. Phase 1 also scored long days-on-market as risk when a
senior partner's stated #1 rationale on the firm's largest recent acquisition was a distressed
basis.

**What it scores:** proximity to holdings (heaviest — the strongest revealed preference, and
exactly checkable), size-in-context (§6's matrix), MOIC-based pricing, distress-as-upside.
Cautions are surfaced, never eliminating.

**The pricing lens is the part that took a rethink.** Vaulter is an opportunistic value-add
*predevelopment* land investor targeting **2.5–3x MOIC** by selling entitled positions to
users and developers. So the question is not "is this priced fairly against comps" — it is
*"at this ask, what must the entitled position sell for to return 3x, and can subdivision and
entitlement get it there?"*

The mechanism matters: **you buy at large-parcel pricing and exit at smaller entitled-parcel
pricing.** A first version compared each listing to same-type peers in the same submarket
regardless of size, and that was wrong — in Pinal, small commercial parcels ask many times
more per acre than large ones, a spread driven purely by parcel size, so 293-acre
assemblages were being scored against 9-acre retail pads and looked like bargains. The fix
compares each parcel to the band it would exit *as*. On the sample export this dropped Tier 1
from 22 listings to 12 and moved the previously top-ranked parcel to rank 17.

**Market-agnostic by construction.** Size bands are absolute (an acre is an acre in Texas), but
every price attached to them is derived from the export itself, walking Submarket → County →
Market → whole file until a cell has enough rows. Feed it Texas, Colorado or Utah and it
recalibrates. A market with no holdings reads as "new market" and the proximity weight drops to
zero rather than penalising every row. Stress-tested against a 20-row export, a synthetic
three-state file, a single-type file and a file with no prices at all: none crash, and the
degenerate cases report **low confidence** or "untestable" instead of inventing a number.

**The time reality is printed on every run.** vaulterup.com publishes **2.40x @5yr, 1.71x
@10yr, 1.61x @15yr**; documented holds ran 12–16 years against 36–48 months underwritten. 3x
over 4 years is 31.6% IRR; the same 3x over 14 years is 8.2%.

| Step | What | Cost |
|---|---|---|
| **Screen** (`fit_screen.py`) | Rank all listings by portfolio fit | Free, instant |
| **Judgment** | Claude reads top candidates **in-conversation** | Free |
| **Ground truth** (`geo_federal.py`, via `verify_listings`) | Flood over the parcel area, roads, place status, relief | Free, keyless |
| **Report** (`report.py`) | One self-contained HTML file next to the workbook | Free |

The four phase modules are **deleted**, not dormant — see §3.2. Phase 3 in particular was
paying the Anthropic API for analysis the asking Claude session does for nothing.

### 3.1 Costs stopped being invented — 2026-07-28 ✅

The pricing lens above was right about *shape* and wrong about *magnitude*, and the wrong
magnitudes were ones this project had made up. A review of the firm's own budget workbooks,
settlement statements and entitlement schedules produced `docs/PORTFOLIO_STANDARD.md` — every
figure with the document it came from — and the screener was rewritten against it.

| Was | Is | Source |
|---|---|---|
| `cost_load`, a flat **0.35 of purchase price** covering entitlement and carry | Gone. Entitlement is priced **per lot** and falls meaningfully with project size, interpolated between measured anchors | Three Arizona budget workbooks — one an invoiced actual |
| `lots_per_acre` **8.0** | **3.5**, with 2.5–4.2 reported alongside anything derived from it | Five measured deals, one outlier excluded (estate lots, much lower density). The two most recent deals are the two lowest |
| Carry folded into the 35% | Charged separately at a measured **1.78%/yr property tax**, over the **observed** hold, not the underwritten one | One real Arizona property's tax history |

Three things about that table matter more than the numbers in it.

**A percentage of purchase price was the wrong shape, not merely the wrong value.** Entitlement
cost tracks lots created and plan sheets a jurisdiction demands. It does not care what the land
cost. Doubling the ask does not double the engineering.

**8.0 lots per acre was roughly double anything in the record**, and it sat in the denominator
of the exit test. `COMPANY_PROFILE.md` §7's "7–9" is stale for the same reason and has not been
corrected there, because that document is an unratified draft nobody has signed off — see §4.

**Horizontal development is deliberately still out of the arithmetic.** Streets, utilities and
grading are measured, real, and per-acre — but only in Pinal County, and the firm sells *entitled*
rather than *improved* land, so the cost applies only when the exit comp happens to be improved
land. Putting a Pinal figure into a Texas row would be inventing a number to avoid admitting an
absence. Instead it is quoted as context on wide-headroom rows, and anything above 4x headroom
is flagged as "the comp is probably improved land" rather than scored as a bargain.

**The rule the whole rework follows: a cost with no record is left out and declared, never
estimated.** Non-residential rows carry no entitlement figure because none exists in the
corpus, and `Cost_Basis` says so on every one of them — the required exit shown is understated.
Ranking *within* a type is unaffected because the treatment is uniform, which is the whole
reason an honest absence is safe and a plausible guess is not.

And because that evidence is overwhelmingly Arizona, every run reports `evidence_coverage` per
state. A Texas export ranks normally and says plainly that there is no Texas cost, timing,
exit-price or rejection history to read it against. Marking an unfamiliar market *down* would
rank the firm's own data coverage instead of the deals — the same bug the neutral proximity
floor exists to prevent.

### 3.2 The 4-phase pipeline is deleted ✅

About 2,500 lines: `pipeline.py`, `phase1_rules.py`, `phase2_ranking.py`,
`phase3_deep_analysis.py`, `phase4_verification.py`, `workbook_builder.py`, `scoring_config.py`,
`market_utils.py`, `dashboard_server.py`, and the screening-local `system/config.py`. Once
`screen_listings` moved to `fit_screen`, every one of them was reachable from nothing.

Two pieces were kept rather than deleted:

- **Phase 4's ground truth** lives on in `geo_federal.py`, which checks flood over the parcel's
  **area** rather than its centre point. That difference caught a real wrong answer: an 80-acre
  listing whose centroid read "Zone X, minimal hazard" had an AE Special Flood Hazard Area
  across part of the parcel.
- **Phase 3's qualitative pass** moved into the conversation, where it costs nothing.

`get_screening_rules` and `test_screener` were repointed at `fit_screen` in the same commit.
They had been reading the deleted hard rules, so they answered "what rules does the screener
use?" with a rulebook that had not run in weeks — worse than dead code, because it was
confidently wrong.

`dashboard_server.py` was retired for two independent reasons, both worth remembering: it read
Phase1/Phase2/Phase3/Phase4 sheet names the current screener no longer writes, so it displayed
nothing at all; and it ran an HTTP server on a background daemon thread, the last one in the
codebase. `report.py` writes a single self-contained HTML file with its data inlined, which
needs neither, and a colleague can open it straight from OneDrive.

### 3.3 The export shape is discovered, not assumed — 2026-07-28 ✅

Every recent bug surfaced on the same file: `system/data/drop/CostarExport (2).xlsx`, a thin 24-column
Tucson template with no coordinates, no days-on-market, and a parcel size on 5 rows out of 50.
Not a malformed file — just a different CoStar template than the 216-row Phoenix export
everything had been built against.

Two changes came out of it, and both belong in any code that reads someone else's spreadsheet:

**Find the header wherever it is.** Exports arrive with a title line, a filter summary and a
blank row above the real column names. `_header_row()` scans the first 12 rows for one that
looks like a header — several non-empty cells, nearly all distinct — instead of assuming row 1.
On a CSV it uses `skiprows`, not `header=`, because `header=3` still makes pandas parse the
three junk lines above and infer column dtypes from them.

**Resolve each field from whatever the file provides, and refuse rather than guess.**
`normalise_columns()` matches by pattern *and* by value range, and returns a `column_sources`
record of what it found, what it derived, what it renamed and what was simply absent. That
record travels into the HTML report, so a thin file announces itself above the fold instead of
reading exactly as confidently as a complete one.

Refusing is the load-bearing half. A `Price/Acre` column matched the weak `price` pattern and
won the asking-price slot outright — a per-acre figure used as the total purchase price, with
every downstream number wrong and nothing saying so. A `Lot Size` column holding square feet
for half-acre pads passed the old plausibility ceiling and read as 29,000 acres. Both now
resolve to nothing and those rows abstain, which is the correct output.

`system/scripts/check_screener.py` is the only automated safety net in the repo and now runs **68
assertions** against deformed market shapes (it was 41 before this work). It also accepts an
export path — `python system/scripts/check_screener.py "system/data/drop/CostarExport (2).xlsx"` runs it
against the thin template, 58 assertions with 2 skipped for data that file genuinely does not
carry. Until 2026-07-29 it crashed on that file before the third check, which is precisely why
the last several rounds of bugs got as far as they did.

### 3.4 The one performance rule ⚠️

`run_mcp_server()` imports pandas (and `corpus`) **before** `mcp.run()` starts the event loop.
Do not make it lazy again.

Every tool in `system/mcp_server.py` imports lazily, which keeps CLI startup quick. Under stdio that
meant numpy's C extension was first loaded *inside* a running asyncio loop, on the first tool
call, and on Windows that stalls for minutes — a stack dump caught the main thread parked in
`numpy/_core/multiarray.py` at `create_module`. The same import takes 0.2s in a plain process.
To the user it looked like a dead server: Claude Desktop reported "the MCP server isn't
responding" and suggested a restart, which never helped, because the next first call paid the
same cost again. Warming it up front costs about a second of startup, once, off the loop.
Fixed in commit `ffcb43f`.

---

## 4. The standard became a document — resolved 2026-07-28, differently than planned ✅

**Status: closed. Read this before proposing the plan below.**

The original plan was: get the buy box out of `system/analysis/screening/config.py` and
`scoring_config.py`, into a document a partner could read, and then have the code read that
document back. The first half happened. **The second half was deliberately not built, and
should not be.**

`docs/PORTFOLIO_STANDARD.md` is where the evidence now lives — the measured costs, hold
periods, realised returns, lot yields, declined deals, and what the record cannot say, each
figure carrying the document it came from. It is the source the 2026-07-28 cost rework (§3.1)
was written against.

**No code reads it, and that is the decision, not an omission.** The user's reason, verbatim:

> *"No one will be touching any document. No one will be touching any code as well."*

Which is worth sitting with, because it inverts the premise. The plan below assumed a partner
would maintain a standard document and the code would follow it — a config file wearing prose.
But nobody is going to edit a markdown file any more than they were going to edit
`scoring_config.py`, and a document the code parses is a config file with worse error messages:
it can be edited into something the parser silently misreads, and there is no syntax error to
catch it.

So the split is: **`PORTFOLIO_STANDARD.md` is the audit trail, `ASSUMPTIONS` in `fit_screen.py`
is the running configuration**, and every value in `ASSUMPTIONS` carries a comment naming the
document, deal and figure it came from. A reader can check the code against the record. A
changed number requires a code change, which is reviewable, testable against
`check_screener.py`, and reaches everyone through the update path in
`MULTI_USER_TRANSITION.md` Priority 4. The document does not move on its own and cannot drift
away from what actually runs.

`docs/COMPANY_PROFILE.md` remains what it always was — **derived from documents, ratified by
nobody** — and is now partly superseded by the measured evidence (its §7 lot yield of "7–9" is
one of the numbers `PORTFOLIO_STANDARD.md` corrected to 3.5). It has deliberately not been
edited to match, because it is a draft awaiting human sign-off, and quietly correcting a draft
nobody has read yet just moves the unratified guess to a new number.

<details>
<summary><strong>The original §4 plan, kept for the reasoning rather than the instruction</strong></summary>

The buy box then lived in `system/analysis/screening/config.py` (hard rules) and `scoring_config.py`
(weights). Nobody but a developer could read it, challenge it, or update it.

Split it in two, because **the halves fail differently:**

- **Disqualifiers** — absolute, eliminate the listing. A wrong disqualifier is *invisible damage*:
  it silently kills deal flow and nobody sees what was cut. Requires human sign-off on every change.
- **Preferences** — weighted, scored, never eliminating. A wrong weight just misranks something
  you'll notice. Safe to iterate freely.

**Numeric thresholds derived from prose are the specific danger.** If Claude infers "minimum 20
acres" when the real answer is 12 in some counties, that disqualifier deletes good deals forever
and nothing surfaces the error.

- Never let a *derived* number become a disqualifier without ratification. Preferences are fine.
- Phase 1 should always report near-misses: *"6 listings failed only on acreage, by <15%"* — so a
  miscalibrated threshold announces itself.
- Your documents describe deals you *did*. That's a biased sample; what you passed on is often
  undocumented.

Three additions worth building alongside it:

1. **Log overrides back into the standard.** When a human says "pursue this, the screener was
   wrong," append a dated note. Highest-compounding accuracy gain over 12 months.
2. **Conversational interface, workbook as record.** *"42 of 318 passed. Here are the top 10.
   Want me to know why the 276 were cut?"* — not "open this 4-tab xlsx."
3. **Show closest analogs, not just scores.** "Resembles a deal you pursued more than the 40 you
   passed on" persuades a partner better than a composite score.

</details>

**Of those three, #2 arrived by a different route** — the screener answers into the
conversation, `screen_listings` returns a ranked summary Claude reads aloud, and `report.py`
writes the workbook-plus-HTML as the record rather than as the interface. #1 and #3 are not
built. #1 is still the highest-compounding idea in this document; it is also the one that
needs somebody to actually disagree with a ranking in writing, which has not happened yet.

**Closed: `.msg` support 🛑** — see §0's 2026-07-29 update. `read_document` handles PDF, Word,
Excel, CSV and text and still does not handle `.msg`. It was only ever justified as the cheap
substitute for the retired email pipeline; with email retired at the source by choice, the
justification went with it.

---

## 5. Area intelligence — trajectory, not proximity 🔨

Proximity = what physically exists nearby *today*. **Trajectory = is this area moving toward
development, and is something knowable already in motion.** For raw land, trajectory usually
matters more.

### 5.0 First, the proximity half — and the wrong answer it was giving ✅

`system/pipeline/proximity_tool.py` is the today half, and it works: one Overpass query returns every
POI category at once within a radius, classified locally, exported as CSV + XLSX to the shared
folder. It has two entry points — by portfolio property name, and by rank from a screen — and
both produce the same format so a candidate and an owned property compare directly.
`compare_proximity_to_portfolio` puts them side by side in one answer.

**By name refuses if the property has no hand-verified coordinate.** Do not add a
geocode-the-name fallback. It was measured at 5 wrong out of 8, two in the wrong country, and
it fails silently. `property_coordinates.csv` has 44 of 49 properties, all read off deeds.

**The bug worth remembering (found and fixed 2026-07-29).** An in-town Avondale listing
reported *"0 results found"* after 154.8 seconds. The true answer at that coordinate is 1,200
features. Three things had to line up:

- `overpass.osm.ch` is a **Switzerland-only extract**, not the overloaded mirror the code's own
  comment claimed it was. A two-point probe settles it: the same trivial query returns 0
  elements in 0.6s at Avondale, Arizona and 20 elements in 1.1s at Bern. It is therefore wrong
  for **100% of this firm's queries, permanently** — and being by far the fastest host, its
  confident empty won the race whenever the real mirrors were slow.
- The production call site passed no `empty_is_suspect` flag, so the one protection that
  existed was switched off at the only place it mattered.
- Two of the three configured mirrors were completely dark from this network. Baseline over 25
  probes: **1 correct answer in 25.**

Fixed by quarantining the Swiss extract (self-enforcing — re-adding the host is a no-op), adding
a coverage-verified planet mirror, flipping `empty_is_suspect` to default **true**, replacing
host-major retries with round-robin passes plus a 600s per-host cooldown, and adding a
**versioned, TTL'd** on-disk cache that never caches an empty or a failure. Measured after:
12 of 12 real listings answered, median 11.6s.

Three rules that fell out of it, all of which generalise past Overpass:

1. **A fast confident empty is the most dangerous response a provider can give.** "Provider
   unreachable" and "provider says there is nothing there" must stay distinguishable all the way
   to the user, and an empty from an unverified source is not a finding.
2. **Overpass has an in-band failure mode.** HTTP 200, valid JSON, zero elements, and a
   top-level `remark` — a server-side timeout wearing the costume of an answer. Same shape as
   the FEMA in-band error `_get_json` already handles.
3. **Do not "optimise" the 64 flat POI selectors into per-key regexes.** It was tried and
   measured: 6.0s → 26.2s on one query, 10.4s → 30.9s on another, byte-identical results.
   Overpass' exact key=value lookup uses an index the regex form cannot.

### 5.1 Trajectory — the three tiers 🔨

**Tier A — Quantitative baseline.** Census (population/housing/permits), BLS (employment). Both
free-registration keys. This is the part that can be a *number* in the model.

⚠️ "Free-registration keys" is still a key. Read the no-keys convention in `CLAUDE.md` before
adding one: adding a key back means adding a dependency on somebody's account, so look for the
keyless equivalent first. Census and BLS both have unauthenticated endpoints with lower rate
limits, which for a research pass run quarterly is likely enough.

**Tier B — Jurisdiction dossiers** *(the key structural idea)*. Comp plans, CIPs, and zoning change
annually but apply to *every* listing in that jurisdiction. Research once, store in the shared
folder, refresh quarterly:

> ✅ **One exists.** `docs/jurisdictions/coolidge-az.md` (local-only), compiled 2026-07-28,
> refresh due 2026-10. Coolidge went first because five of the top ten listings in the July
> 2026 screen sit in or within ~4 miles of it and the firm already holds several properties in
> the same corridor. It is a hand-written document in the repo, not something any code reads —
> same decision as §4, for the same reason.
>
> It also demonstrates why the funded/planned/discussed rule below is not pedantry. Its
> headline is a **granted** 100-year Assured Water Supply designation to the area's water
> utility, dated 2026-03-03 — the first new one in the Pinal AMA in
> two decades, and four months before the export was pulled. Most Coolidge-area listings in
> that export had been sitting 430–1,707 days, i.e. listed *before* the unlock. That is the
> shape of a repricing window. The same dossier then spends a paragraph on why not to
> over-read it: the designation is to the water *company*, not to any parcel, and sewer — a
> 2 MGD lagoon plant — did **not** unlock.

> **Forney, TX** — future land use designates FM 548 corridor Medium Density Residential (Comp
> Plan 2023, Fig 4.2). CIP FY26–30 funds wastewater extension along FM 548, construction FY27
> (CIP p.47). Forney ISD passed $290M bond Nov 2025. Impact fees +18% Jan 2026.

Same shape as the buy-box standard: expensive research once → durable artifact → cheap reuse.

**Tier C — Agenda monitoring** *(where the edge is)*. Rezonings, plats, and annexations appear on
agendas *weeks* before they're news. Keyword watch: rezone, plat, annex, utility extension, CIP
amendment, MUD.

⚠️ Note there is now no scheduler to hang Tier C on. It needs either a Windows Scheduled Task on
one designated machine, or to run on demand. Do not resurrect the in-process scheduler thread for it.

### Two non-negotiable rules

1. **Every signal carries a source and date.** "Forney CIP FY26–30 p.47, retrieved 2026-07-15" —
   not "the area is growing." If it can't be cited, say *unknown*. A fabricated CIP project in an
   investment memo is a serious problem.
2. **Separate funded / planned / discussed.** A discussed road is worth nothing; a funded one with
   a construction year is worth a lot. Easiest thing for an LLM to blur, and most bad land bets
   confuse the two.

### How it drives the decision

|  | Strong trajectory | Flat trajectory |
|---|---|---|
| **Strong site** | Pursue | Land bank / too early |
| **Weak site** | Look harder — may still pencil | Pass |

Today's screener would rank a great site in a dead area above a mediocre site in the path of
growth. That's backwards for land.

**Honest gap:** coverage will be good for incorporated cities on modern agenda platforms and poor
for unincorporated county parcels — often exactly where raw land is bought. Some of that genuinely
needs a human calling the county.

---

## 6. Current structure

*Verified against the tree on 2026-07-29. The earlier version of this section claimed 18 tools,
two optional API keys, and `system/analysis/screening/` as "the 4-phase pipeline — untouched." All
three were wrong; the pipeline had been deleted three commits earlier.*

```
config.py               every path and tunable; NO API keys
main.py                 mcp | index-corpus | search | screen | properties | stats
mcp_server.py           21 @mcp.tool() functions, no background threads,
                        pandas preloaded before the event loop (§3.4)
portfolio.py            Project Master reader (CSV/.xlsx; only .xlsx marks sold)

system/corpus/
├── index.py            SQLite name index, search ranking, resolve_in_corpus() scope guard
└── extract.py          PDF (OCR), Word, Excel, CSV, text — one file at a time, never in bulk

system/analysis/screening/
├── fit_screen.py       THE LIVE SCREENER — portfolio-fit ranking, ASSUMPTIONS at the top
├── geo_federal.py      ground truth: FEMA flood over the parcel AREA, Census TIGER, USGS
├── geo_providers.py    keyless geodata + the Overpass mirror/cache layer
├── report.py           builds the self-contained HTML report
└── report_template.html

system/pipeline/
├── proximity_tool.py       one Overpass query, all POI categories, CSV + XLSX to the shared folder
└── property_coordinates.py hand-verified coordinates, 44 of 49 properties, from deeds

system/core/safe_io.py         atomic writes, locking, conflict-copy merge
system/scripts/                release, apply_update, push_org_setting, setup_wizard, check_screener
quick_start/            two double-clickable setup launchers (.bat / .command)
docs/                   this file, PORTFOLIO_STANDARD, COMPANY_PROFILE,
                        MULTI_USER_TRANSITION, jurisdictions/
system/data/                   drop/, project_master/, pending_update/, pending_settings/,
                        corpus_index.db, logs/
```

Gone since the last revision of this section: `system/analysis/screening/pipeline.py`,
`phase1_rules.py`, `phase2_ranking.py`, `phase3_deep_analysis.py`, `phase4_verification.py`,
`workbook_builder.py`, `scoring_config.py`, `market_utils.py`, `dashboard_server.py`, the
screening-local `system/config.py`, and `system/analysis/screening/dashboard/vaulter_dashboard.html`.
See §3.2.

---

## 7. Open questions

Closed since the last revision — kept as answers, not questions:

- ~~**`.msg` support?**~~ 🛑 Decided against. §0's 2026-07-29 update.
- ~~**Who rebuilds email, and as what?**~~ 🛑 Nobody. The inbound flow is being retired at the
  source. §0.
- ~~**Does the standard become a document the code reads?**~~ ✅ Half of it. The document
  exists; the code deliberately does not read it. §4.

Still genuinely open:

1. **Index freshness.** The index is a local snapshot; new documents are invisible until it's
   rebuilt. `check_system_health` warns past 30 days. A monthly Windows Scheduled Task on one
   machine would close it — but each person's index is their own, so it needs to be per-machine
   or it closes nothing.
2. ~~**Confirm the shared folder name across the team.**~~ ✅ **Partially resolved 2026-07-29** —
   this item actually bundled two different risks with different severity:
   - `SHARED_DIR` ("Vaulter AI Shared") is created by this app itself, with a name this code fully
     controls. It was never actually at risk of a naming mismatch -- every install creates it
     fresh with the identical literal name, so "screening output went somewhere nobody can see"
     was never really a naming problem.
   - `CORPUS_DIR` (the firm's own SharePoint library) is a pre-existing SharePoint library
     synced with a name nobody here controls, and it genuinely does vary -- confirmed
     colleagues see slightly different casing and prefixing on the same underlying library.
     `config._find_corpus_subfolder()` now matches a distinctive fragment of the library's own
     name case-insensitively instead of one exact string, and refuses -- rather than guessing --
     if more than one folder matches, same "pattern, then refuse on ambiguity" rule the CoStar
     column resolver uses.

   **Still genuinely open:** `ONEDRIVE_FOLDER_NAME` (the account root's own name) -- is still
   an exact-match assumption, unverified on an actual second machine.
   Confirmed consistent across the team as of 2026-07-29 (standard OneDrive-for-Business naming,
   tied to the org name), which lowers the risk, but doesn't close it.
3. **Does the team's plan include 1M context?** Determines the practical ceiling for how much of
   a long document can go into one conversation.
4. **Does the screener read `In SFHA`?** *(New, 2026-07-29 — has measurements behind it and no
   owner.)* Both real CoStar exports carry the column: Arizona says Yes on 46 of 216, Tucson on
   10 of 50. `add_cautions` currently reads `Flood Risk Area`, which matches `In SFHA` exactly
   on the Arizona file but is **absent from the Tucson template** — so 10 Tucson listings inside
   a Special Flood Hazard Area currently get no flood mention at all. Separately, `Floodplain
   Area` is a *text label* ("500-year Floodplain") on both files rather than a number, so the
   net-acreage branch in `add_cautions` has never once executed. Reading the label instead was
   implemented, measured and **rejected**: it fires on 161 of 216 Arizona rows and, sitting in
   the same if/elif chain, shadows the precise signal. This is a standard question — §5 of the
   profile documents the firm buying through flood repeatedly — not a code question. Do not
   ship the label-quoting version.
5. **Is the smallest anchor's entitlement figure the right floor for a genuinely small
   project?** It is the figure from the smallest measured project, and
   `PORTFOLIO_STANDARD.md` (local-only) §5.5 flags that same project's estate-lot density
   as a low-density outlier. The anchors are flat below the smallest measured
   project's lot count, so every small project pays the outlier's rate.
6. **Should a sub-20-acre listing be in the headroom test at all?** Its exit band maps to its
   own band, so it is scored against a median it is part of, and its entire modelled lift has
   to come from entitlement already being in place. It does not occur on either real export
   and the row does say so, so nothing was changed — but the alternative (report "no
   subdivision exit" and abstain) may be the more honest output.

---

## 8. Order of work, as the user set it (2026-07-28)

Stated plainly, and it does not match the order the rest of this document is written in:

> **1. Finish screening and proximity. 2. Then installability.**

Which means §5's Tier A/B/C, the analog-matching in §4, and everything in
`MULTI_USER_TRANSITION.md` Part F wait. The two things in flight are the two things that
produce an answer somebody acts on.

**What "finish" looks like for each.** For screening: the open questions at §7.4–7.6 are the
remaining known-unknowns, and the honest gap is that `check_screener.py` covers `fit_screen.py`
thoroughly and covers `report.py` not at all — the report reads screener columns by name and
degrades to blank fields if one is renamed, which is precisely how it fell a version behind
before. For proximity: it works and is fast now, but it depends on volunteer-run endpoints, so
the failure mode to keep watching is a *plausible* answer rather than a missing one.

**Then installability**, which is `MULTI_USER_TRANSITION.md` Priority 3 and 4 — the only two
priorities in that document that survived the rebuild. Priority 4 is built; Priority 3 is
built and untested on anyone else's machine, and §7.2 above is the specific thing most likely
to bite.
