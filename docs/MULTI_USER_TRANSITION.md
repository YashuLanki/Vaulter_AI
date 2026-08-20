# Vaulter AI — Multi-User Transition Analysis & Roadmap

**Date:** 2026-07-21 · **Status annotated 2026-07-29**
**Scope:** A grounded review of the current codebase after the recent bug-fix work, plus a
prioritized plan for moving from one user to a whole team (~5–15 non-technical real-estate
staff), each running their own fully-local instance.

This document is written to be readable by a non-technical reader. Each section ends with a
short "For implementers" note where there's a concrete technical detail worth preserving.

---

## ⚠️ Read this before anything below it

**This document is historical. It is still the best record of *why* the old design had the
problems it had, and that reasoning is why it has been kept rather than deleted. But most of
the code it analyses no longer exists.**

The 2026-07 rebuild (`docs/REBUILD_PLAN.md`) removed the ingestion pipeline, ChromaDB, the
email reader, the web scraper, the background scheduler and watcher threads, and — later that
month — the entire 4-phase screening pipeline. Roughly half the files named in the Appendix
are gone.

Where each part stands:

| Part | Status today |
|---|---|
| **A** — loose ends A1/A2/A3 | 🛑 **Moot.** All three lived in `ingestion/`, which was deleted. A1's OCR memory spike matters as a *lesson* — see below. |
| **B** — Theme 1 onboarding | ✅ **Addressed.** `system/scripts/setup_wizard.py` + `quick_start/`. Untested on anyone else's machine. |
| **B** — Theme 2 silent failure | ✅ **Built.** `check_system_health`, called once per conversation, silent when healthy. |
| **B** — Theme 3 shared folder | ⚠️ **Reduced, not eliminated.** See Part C below. |
| **B** — Theme 4 versions/data | ✅ **Built.** Auto-update path, Priority 4. |
| **C** — concurrency hazards | 🛑 **Dissolved.** C1/C2 were fixed in `safe_io.py`, then all four shared state files — `manifest.json` included — were deleted. The fixes now have no callers. See the note at the head of Part C. |
| **D** — Priorities 0, 1, 2 | 🛑 **Superseded.** They describe code that no longer exists. |
| **D** — Priorities 3 and 4 | ✅ **Still the roadmap, and both built.** These are the two sections worth reading as instruction rather than history. |
| **E** — risks of each fix | ✅ Still useful. The predictions about the health check being either noisy or silent both held. |
| **F** — meeting transcripts | 🛑 **Dead.** It depended on the Teams connector, which was rejected, and on the ingest pipeline, which was deleted. |
| **G** — shared portfolio documents | ✅ **Resolved differently and more simply.** See the note at the head of Part G. |

**Two things in here are worth carrying forward regardless of the code being gone:**

- **A1's real lesson**, restated in `CLAUDE.md` as a hard rule: *never scan a PDF without a
  timeout.* The OCR fallback that rendered a whole document because one page had no text
  reached **6.5 GB** before it was killed.
- **Part B Theme 2's framing** — that a system built never to crash will look exactly like a
  working system when it is half broken — is the reason `check_system_health` exists and the
  reason it stays silent when healthy. That argument has not aged at all.

---

## Executive summary

- **The recent bug-fix work holds up.** An independent re-audit of everything changed this
  session found **no new serious bugs**. It confirmed the trickiest fixes are correct. There
  are three small loose ends worth tidying (below).
- **Going multi-user is mostly not a code-correctness problem — it's an operations problem.**
  The four things that will actually bite a team rollout are: (1) getting each person set up,
  (2) no way to tell when someone's copy has quietly broken, (3) the shared OneDrive folder is
  fragile when several people use it at once, and (4) keeping everyone on the same version and
  the same portfolio data.
- **The single highest-leverage addition is a "health check."** Almost every other problem is
  currently *silent* — a health check makes them visible.
- **The most technically serious risk is in the shared OneDrive folder**, where the team's
  saved (already-paid-for) screening results can be silently wiped under the right timing.
- **A new capability is planned: searchable Monday-meeting transcripts** (Part F). Teams
  records and transcribes the meeting, the transcript lands in a shared folder, and everyone
  can ask Claude what was said. It reuses the existing search pipeline and is shared (like
  screening) rather than private (like email).
- **Confirmed architecture decision: portfolio documents move to the existing shared OneDrive**
  (Part G). The local `watched_folder` was only ever a test stand-in; in production, portfolio
  documents live in the company's existing OneDrive repository and every person's own instance
  ingests from that same shared source into their own private database. Email stays exactly as
  private as it already is — no change there.

---

## Part A — Checking the recent work *(removed)*

Three loose ends in `ingestion/`, which was deleted in the 2026-07 rebuild. A1's lesson survives as a hard rule in `CLAUDE.md`: **never scan a PDF without a timeout** — the OCR fallback that rendered a whole document because one page had no text reached **6.5 GB** before it was killed.

---

## Part B — Multi-user readiness: the four themes

### Theme 1 — Getting each person set up (biggest friction)
Onboarding today is a developer-grade checklist: install Python, create a virtual environment,
install two separate OCR tools (Tesseract and Poppler), hand-edit config paths, create a
secrets file, run a terminal command to sign into Outlook, and hand-edit Claude Desktop's
configuration file with an exact path. For 10 non-technical staff, that's 10 fragile installs
and a support ticket at every step that goes wrong.

Two specific landmines:
- **Windows secrets trap:** the setup docs tell people to put their secrets in a folder inside
  the project, but on Windows the code reads them from a *different, hardcoded* folder
  (`C:\Users\<name>\Vaulter AI\confidentials`). Someone can "finish setup" and have nothing
  actually load, with no error — every key silently comes back empty.
- **Fragile tool paths:** the locations of the OCR tools are hardcoded to very specific paths
  (including an exact version number in one, and Apple-Silicon-only paths on Mac). Any
  deviation silently breaks scanned-document reading.

*For implementers: `system/config.py` (`SECRETS_DIR`, `TESSERACT_PATH`, `POPPLER_PATH`), `README.md`
setup section, `system/requirements.txt` (dependencies are largely unpinned; the author's committed
environment is on a bleeding-edge Python that may not have prebuilt packages on staff
machines).*

### Theme 2 — Nobody can tell when a copy has quietly broken (highest leverage)

> **✅ Built, and this is the single argument in this document that has aged best.** See
> Priority 1. `check_system_health` now reports library sync, index age, portfolio source, and
> a staged update if one is waiting — Outlook, ChromaDB and the scheduler are all gone from it
> because they are all gone from the system. It runs once per conversation and says nothing
> when healthy, exactly as Priority 1 argued it must.
The system is deliberately built to never crash. The flip side: when someone's Outlook login
expires, their database corrupts, their scheduler jobs keep failing, or their shared-folder
link has silently fallen back to a local folder — **it looks exactly like a working copy.**
Logs go to a local file no one opens. At 10 users, several copies will be half-broken at any
moment and no one will know.

### Theme 3 — The shared OneDrive folder is fragile under simultaneous use
The one genuinely-shared thing is the OneDrive "screening output" folder, which holds the
team's screening results and the record ("manifest") of what's been screened, so the first
person to screen a file saves everyone else from re-paying for it. OneDrive is **not** a real
shared database — it syncs each person's own copy of these files independently. That creates
real hazards when several people use it at once (detailed in Part C).

### Theme 4 — Keeping everyone on the same version and portfolio data
Each person has their own copy of the code and their own copy of the Project Master
(portfolio) file. Code fixes reach someone only if they manually update. Portfolios drift
apart. There's also a frozen, built-in property list that silently takes over if someone
forgets to drop in the real export — so a user can unknowingly run on an aging portfolio with
no error.

*This has since been resolved as a design decision, not just flagged as a risk — see Part G.
It turns out to apply to all portfolio documents, not only the Project Master file.*

---

## Part C — Shared-folder concurrency *(removed)*

Five hazards in code that no longer exists. C1 and C2 were fixed in `safe_io.py`, and then all four shared state files those fixes protected were deleted, so the fixes have no callers. The framing that survives is in `CLAUDE.md`'s own conventions: a shared file can be the wrong **shape**, not just missing or corrupt, because every teammate can write to that folder.

---

## Part D — Prioritized improvement roadmap

Each item is independent and can be done on its own. Recommended order:

> **2026-07-29: Priorities 0, 1 and 2 are closed. Priorities 3 and 4 are built and are the two
> sections here still worth reading as instruction.** Priority 0 is moot (Part A). Priority 1 is
> built. Priority 2 dissolved rather than completing — the fix landed, then the files it
> protected were deleted (Part C).

### Priorities 0–2 *(removed)*

Superseded. They describe code that no longer exists. Priority 1's argument did survive and became `check_system_health`: **a system built never to crash looks exactly like a working system when it is half broken**, which is why that check exists and why it stays silent when healthy.

---

### Priority 3 — Easy onboarding *(unblocks the actual rollout)*

> **✅ Built 2026-07, and this section is still the specification it was built to.**
> `system/scripts/setup_wizard.py`, launched by `quick_start/Setup Vaulter AI.bat` / `.command`, does
> the whole list below: creates the environment, installs dependencies, detects Tesseract and
> Poppler by searching rather than hardcoding (and says in plain English what is missing and
> how to install it per-user), copies `system/confidentials/.env.template` into place, **merges** its
> entry into whatever `claude_desktop_config.json` already exists without touching another MCP
> server, and finishes by building the document index. It reports each step rather than
> assuming success, and is safe to run twice.
>
> **Three things this section predicted that turned out differently:**
>
> - **The sign-in step is gone entirely.** This section calls sign-in "the **only** step that
>   can't be pre-baked." That was true when Outlook was in the picture. With email dropped and
>   every API key removed, onboarding is: install Python, download the code, double-click, drop
>   the Project Master in, restart Claude Desktop. There is nothing to sign into and nothing to
>   paste. The advice below about baking org-wide keys into the installer is therefore moot —
>   there are no keys.
> - **The Windows secrets trap was fixed by accepting both locations, as Part E recommended.**
>   `SECRETS_DIR` is the project's own `system/confidentials/` on every OS; Windows also checks the
>   legacy `C:\Users\<name>\Vaulter AI\confidentials` — but *only* if that path has a real
>   `.env` and the project folder does not, so the one pre-existing setup keeps working without
>   being switched out from under it.
> - **It is untested on anyone else's machine.** The specific thing most likely to break is
>   OneDrive folder detection (`REBUILD_PLAN.md` §7.2), and it fails as "screening output went
>   somewhere nobody can see" rather than as an error.

A guided installer/bootstrap that: creates the environment and installs dependencies, finds
the OCR tools automatically (or clearly says what's missing), pins a known-good Python version,
fixes the Windows secrets-folder trap, and sets up the exact Claude Desktop configuration
automatically. The goal: a staffer follows one guided flow, and it *verifies* each step instead
of failing silently later. The realistic end state for a non-technical user is **sign-in only**
— everything else below is what makes that possible.

**Bake in the values that aren't actually per-user secrets.** The Outlook client ID and the
Anthropic/Google API keys are organization-wide values (one Azure app registration, one set of
API keys for the whole team) — not something each person needs to look up or paste in. They can
be embedded directly in the installer package IT builds once, so a staffer never sees an API key
or client ID at all. The **only** step that can't be pre-baked is the person proving their own
identity — the actual Microsoft sign-in (email + password + MFA) — which isn't really "technical
setup," just logging in.

**Assume unmanaged machines — no Intune/MDM.** IT was asked whether Vaulter's laptops are
provisioned/imaged (which would let Python + Tesseract + Poppler simply ship as part of a
standard image, handled once by IT instead of repeatedly by each non-technical user). That
request has stalled with no response, so the installer must **not** depend on it — design
onboarding entirely around per-user, no-admin-required installs on whatever machine a staffer
already has:
- Python's official installer has a per-user "install for me only" option that needs no admin
  (only its system-wide "for all users" option does).
- Poppler isn't an installer at all — it's just a folder of binaries to unzip; nothing to
  install or elevate.
- Tesseract's Windows installer also offers a non-admin, per-user install option.
- If IT-managed imaging ever does materialize later, it's a pure bonus that removes this step
  for future hires — but the installer can't be designed to depend on it.

**Don't clobber an existing Claude Desktop setup.** Some staff may already have Claude Desktop
installed (possibly with other MCP servers already configured for unrelated tools). The installer
must not assume it owns that file — it needs to find the existing
`claude_desktop_config.json`, and **merge** the Vaulter server entry into its `mcpServers` block
rather than overwriting the whole file. If Claude Desktop isn't installed at all, the installer
should simply point the user to install it first, since that's a separate app outside this
project's control.

*For implementers: the merge logic belongs in the same installer script that handles the
Python/OCR setup — read the existing JSON if present, add/update only the Vaulter entry under
`mcpServers`, and write it back preserving every other key untouched.*

### Priority 4 — Version & shared reference data

> **✅ Built 2026-07, and it works differently from the sketch below in one important way.**
>
> `system/scripts/release.py` (run by whoever ships a reviewed fix, never by staff) packages the
> current code — excluding `system/confidentials/`, `system/data/`, any virtualenv and `.git` — into a zip and
> publishes it with a version marker to the shared OneDrive folder. **Staged rollout is real:**
> `python system/scripts/release.py` publishes to the `canary` channel only; `--promote` copies that
> same already-published version's marker to `general` once it is confirmed healthy. Each
> instance reads its own `VAULTER_UPDATE_CHANNEL` from `.env`, defaulting to `general`.
>
> **The check does not run on a scheduler thread, because there is no scheduler thread.** It
> runs once a day from `check_system_health`, which Claude already calls at the start of every
> conversation. Same cadence in practice, no thread. The sketch below assumes the background
> scheduler "that already runs continuously on every instance" is the natural home for this;
> that thread was deleted in the rebuild and must not come back.
>
> **Applying is a conversation, not a restart.** The sketch says apply "on next restart," which
> is the Slack/Chrome pattern. What was built is confirm-then-apply *in chat*:
> `check_system_health` surfaces a staged update, Claude asks, and if the user says yes the
> `apply_pending_update` tool syncs the files into place and **re-runs `pip install -r
> requirements.txt` with the same interpreter already running the project** — so a fix that
> changes a dependency doesn't leave the app broken for want of an uninstalled package. This is
> deliberately not fully automatic, given the "could break every instance at once" blast radius
> the section below correctly identifies. The human decision just happens in chat instead of a
> terminal. The one step that cannot be automated at all: fully quitting and reopening Claude
> Desktop afterward, since an MCP server cannot restart its own parent application.
>
> **One invariant for whoever touches this next:** `system/scripts/apply_update.py`'s
> `PRESERVED_DIR_NAMES` must always match `system/scripts/release.py`'s `EXCLUDED_DIR_NAMES` exactly.
> The apply step trusts that anything under those paths was never in the package to begin with,
> so it never deletes or overwrites them.
>
> **What was NOT built:** the format-version stamp on the shared results record. It was, briefly
> — `manifest.json` carried a `MANIFEST_FORMAT_VERSION` and readers ignored entries stamped
> higher than they understood. Then the manifest itself was deleted along with the paid caching
> it existed for (see Part C). There is no shared read-modify-write state left to version.
> `system/scripts/push_org_setting.py` distributes org-wide settings by the same publish-and-stage
> route as code, applied via `apply_pending_settings`.

Stamp a version on the code and on the shared results record so newer and older copies can't
corrupt each other, and add a simple update path (see below). The portfolio-documents-on-OneDrive
piece originally scoped here (move the Project Master to the shared folder) has been superseded
by the broader, confirmed decision in Part G — implement that instead of this narrower version.

**Why this matters more once bug-fixing is a real workflow.** The plan is: errors/bugs from
across the team get collected somewhere central, a human periodically points Claude Code at
them to investigate and fix, and the fix gets reviewed before merging (see the discussion
above — deliberately NOT a fully autonomous pipeline that ships unreviewed fixes on its own).
But a reviewed fix sitting in the codebase doesn't help anyone until it actually reaches their
machine — and today, nothing does that automatically. Each person's instance is a fully
independent local copy; a fix only reaches someone if they manually pull new code and
reinstall. **This is a real, currently-missing piece, not just a nice-to-have.**

**How auto-update would work for this architecture specifically.** Since there's no shared
server to redeploy once (each person runs their own independent local copy), "updating
everyone" means each person's own instance has to notice a new version exists and pull it down
itself:
- The background scheduler thread that already runs continuously on every instance (currently
  handling email/web scraping) is the natural place to also periodically check a central
  "what's the latest version" marker.
- If a newer version exists, download it quietly in the background — no popup, no action
  needed from the user.
- Apply it **on next restart**, not by hot-swapping code while the server is actively running
  — the same pattern Slack/Chrome/most auto-updating apps use, and much safer than patching a
  live process.

**The safeguard this needs: don't let a bad update break everyone at once.** Auto-updating
immediately for the whole team means a subtle problem in a fix breaks every single person's
copy simultaneously instead of just one. Two reasonable mitigations:
- **Staged rollout ("canary"):** ship a new version to one or two people first, confirm it's
  healthy, then let it reach everyone else — rather than pushing to all 10+ instances the
  moment it's merged.
- **Tie it to the health-check tool (Priority 1):** if errors spike right after an update goes
  out on the canary machines, that's an early warning before it reaches everyone — the health
  check becomes the safety net that catches a bad auto-update before it does wide damage.

*For implementers: the version marker and the update package itself both need somewhere to
live that every instance can reach — the same shared OneDrive location already used for
screening output (Part C) and portfolio documents (Part G) is the natural fit, keeping this
consistent with the rest of the shared-state design rather than introducing a new channel.*

---

## Part E — Bugs each improvement could introduce, and how to prevent them

This is the "what could go wrong with the fix itself" analysis.

**Health-check tool**
- *Risk:* it becomes a maintenance burden, or raises false alarms that erode trust.
- *Prevention:* keep it strictly read-only and derive everything from state that already
  exists (database counts, the token file's timestamp, the scheduler's last-run record). It
  should be incapable of changing anything, so it can never itself cause a problem.
- *Risk:* making it proactive (run automatically, not on request) turns a once-harmless check
  into something that talks in every single conversation, becoming exactly the kind of noise
  users learn to ignore — which defeats the purpose just as badly as silence does.
- *Prevention:* only speak up when something is actually wrong; stay completely silent when
  healthy. Run once per conversation, not per message.

**Shared-folder safety**
- *Risk:* an over-aggressive "refuse to write" rule blocks legitimate saves and makes screening
  feel broken.
- *Prevention:* the refusal must be narrow — only when a file is *present but unreadable*
  (mid-sync), never when it's legitimately empty or absent. Pair it with the conflict-copy
  merge so anything that does slip through is recovered automatically rather than lost.
- *Risk:* the "in-progress" marker gets left behind after a crash and permanently blocks a file
  from being screened.
- *Prevention:* give markers a short expiry (e.g. 15 minutes) and always remove them in a
  cleanup step even if the run fails.

**Easy onboarding / installer**
- *Risk:* it works on the author's machine but not on varied staff machines (different OneDrive
  folder names, Intel vs Apple Macs, OCR tools installed in the standard location instead of
  the hardcoded one).
- *Prevention:* detect tools and folders by searching the system rather than hardcoding, pin a
  widely-supported Python version, and have the installer *check and report* each step in plain
  English rather than assuming success.
- *Risk:* fixing the Windows secrets-folder path breaks the one machine already relying on the
  old location.
- *Prevention:* make the code accept either location (prefer the project folder, fall back to
  the old hardcoded one) rather than switching outright.

**Version & shared reference data**
- *Risk:* an update lands mid-task, or a new version reads an old shared-data format and
  mis-reads it.
- *Prevention:* stamp a format version on the shared results record; a copy ignores entries it
  doesn't understand rather than trusting them. Only check for/apply updates at startup, never
  mid-run.
- *Risk:* moving the Project Master to the shared folder means one bad export breaks everyone at
  once.
- *Prevention:* keep the local drop-in as an override, and have the health check report which
  portfolio source and date is in effect so a bad update is caught immediately.

---

## Part F — Searchable meeting transcripts *(removed)*

Dead. It depended on the Teams connector, which was rejected, and on the ingest pipeline, which was deleted. See `docs/REBUILD_PLAN.md` §0 for the connector verdict.

---

## Part G — Confirmed architecture: shared portfolio documents via the existing OneDrive

> **✅ 2026-07-29: right conclusion, and the implementation turned out to be much smaller than
> the section anticipated.** Portfolio documents are indeed shared team data read from the
> firm's existing OneDrive repository — but *nothing is ingested*. `CORPUS_DIR` points straight
> at the firm's own SharePoint library and `system/corpus/` reads it in place. No watcher, no
> per-person copy, no local database of contents.
>
> **The "open question to resolve" at the end of this section — how to map the real folder tree
> onto `<State>/<Property>` — never had to be answered.** It was a question the *ingest*
> pipeline forced, because state/property/category had to be stamped onto every chunk at ingest
> time. Reading files in place asks nothing of the folder structure. As it happens the library
> does follow `!PROPERTIES/<STATE>/<Property>/...`, which is why `open_property_files` works —
> but if it hadn't, search would still work, because it matches names and paths.
>
> **Two things this section got right that are now load-bearing rules:** portfolio documents are
> shared and screening output is shared, but the *privacy boundary* moved rather than
> disappeared. `CORPUS_DIR` is that library specifically, **never the OneDrive account root
> one level up** — that root also holds the individual's own Desktop, Documents and Teams chat
> files. `corpus.resolve_in_corpus()` resolves and re-checks every path. That guard is what the
> per-user-database architecture used to provide.
>
> The email row of the table below is now moot: there is no email pipeline.

**This is a settled decision, not a proposal.** It confirms and generalizes what Theme 4 (Part B)
had already flagged as a risk — that portfolio reference data was being forced into per-user
state — and supersedes the narrower "just the Project Master" framing originally in Priority 4.

### The confirmed data-flow model

| Data | Source | Destination |
|---|---|---|
| **Portfolio documents** (due diligence PDFs, financials, and the Project Master itself — everything currently dropped into `watched_folder`) | The company's **existing** OneDrive document repository — already in place today, already the team's real source for these documents | Every person's own **local, private** ChromaDB. Everyone ingests the same shared source independently. |
| **Email and anything derived from it** (attachments, etc.) | Private per person (their own Outlook) | That person's local database **only** — never shared, never visible to a colleague's instance. No change from today. |
| **Screening results** (combined workbook + analysis) | Generated by whoever runs `screen_listings` | Written back to the shared OneDrive (already the existing, working design — see `SCREENING_OUTPUT_DIR` in `system/config.py`) so the whole team benefits from one run instead of each person re-running it. |

The privacy boundary is unchanged and remains load-bearing: each staff member runs a complete,
independent local instance (own ChromaDB, own Outlook auth), so email never touches anyone
else's machine. What changes is that portfolio *documents* — like screening output already
does — are recognized as shared team data, not personal state, and get treated that way.

### What this means concretely
- **`system/data/watched_folder/` was only ever a local test stand-in**, not a production design. In
  production, the thing each person's watcher watches for new portfolio documents should be
  the shared OneDrive folder (the same one the team already uses today for "portfolio, money,
  etc."), not a folder that only exists on one machine.
- Future documents dropped into that shared OneDrive folder by anyone should flow into
  **everyone's** local database automatically, the same way a screening run today benefits
  the whole team once it lands in the shared output folder.
- Nothing about the email pipeline changes. This confirms, not revises, the existing privacy
  design.

### The open question to resolve — deliberately not decided here
The current ingestion pipeline expects portfolio documents to sit in a very specific shape:
`<State>/<Property Name>/file.pdf` (see `ingestion/watcher.py`'s header) — this is how state,
property, and category get auto-tagged onto every chunk. The company's **existing** OneDrive
repository predates this project and was **not** necessarily built to that shape.

Before any implementation, this needs a real look at the actual existing OneDrive folder
structure, not an assumption:
- If it already happens to follow (or can trivially be read as) `<State>/<Property>` —
  the fix is close to a pure config change: point the watcher at that path instead of the
  local `WATCH_DIR`, using the same OneDrive-detection pattern `system/config.py` already has for
  `SHARED_DIR`/screening output.
- If it's organized some other way (by deal name, by year, flat, by document type) — the
  watcher's folder-parsing logic (`_resolve_from_path`) needs to adapt to however it's actually
  organized, which may mean deriving state/property some other way (e.g. matching document
  content or filename against the Project Master, closer to how `property_matcher.py` already
  works for email/web content) rather than assuming a folder-path convention that may not hold.

*For implementers: don't guess at this — the first real step of implementing Part G is looking
at the actual existing OneDrive folder tree and deciding which of the above (or some blend)
applies, before touching `system/config.py` or `ingestion/watcher.py`.*

### Why this is comparatively low-risk on the concurrency front
Unlike the Part C hazards (which are about many people reading/writing the *same shared JSON
state file* — the manifest, the caches), portfolio documents are ordinary independent files.
Two people rarely write the exact same document at the exact same instant, and each person's
*read* side (their own local watcher ingesting into their own local database) never writes
back to the shared folder at all. So the OneDrive conflict-copy and last-writer-wins hazards
in Part C mostly don't apply here — the shared-state risk is specific to the screening
manifest/caches, not to this.

### Suggested priority placement
This is a bigger lift than Priority 0–1 but more foundational than Priority 2–4 once the folder
structure question above is resolved — it's arguably what makes the system feel like a real
shared team tool rather than a personal one. A sensible place in the Part D sequence: right
after Priority 1 (health check), before Priority 2 (shared-folder safety), since the health
check should already be reporting whether the shared folder is properly connected before this
lands.

---

## Appendix — Key files referenced

> **2026-07-29: most of this list no longer exists.** Kept verbatim so the analysis above stays
> readable, with a status marker on each. **Gone:** every `ingestion/` file, `rag_engine.py`,
> `property_scraper.py`, `property_matcher.py`, `outlook_auth.py`, `scheduler.py`, and the
> screening `pipeline.py` / `phase3_deep_analysis.py` / `phase4_verification.py` /
> `workbook_builder.py` / `dashboard_server.py` / dashboard HTML.
>
> Where to look instead: `system/corpus/index.py` and `system/corpus/extract.py` replace the whole ingest and
> retrieval stack; `system/portfolio.py` replaces `property_scraper.py`'s reader half;
> `system/analysis/screening/fit_screen.py` replaces the four phases; `system/analysis/screening/report.py`
> replaces the dashboard; `system/analysis/screening/geo_federal.py` carries forward Phase 4's ground
> truth. `system/scripts/check_screener.py` — which did not exist when this was written — is now the
> only automated safety net in the repo.

- `system/config.py` — paths, shared-folder detection, OCR tool paths, the scraping on/off flag.
  *(Still exists. The scraping flag does not — there is no scraper. No API keys either.)*
- `safe_io.py` — the shared file read/write layer (atomic writes, same-machine lock). The
  same-machine-only limitation is the root of the Part C hazards and is documented in its
  header.
- `system/analysis/screening/pipeline.py` — the shared manifest/cache logic and the cost-saving
  cache check.
- `system/analysis/screening/phase3_deep_analysis.py`, `phase4_verification.py` — per-listing and
  per-finalist shared caches; the Claude calls that cost money.
- `system/analysis/screening/workbook_builder.py` — writes the result spreadsheets (non-atomically).
- `system/analysis/screening/dashboard_server.py` + dashboard HTML — the shared results viewer.
- `ingestion/extractor.py` — PDF/Excel text extraction (A1 OCR item).
- `ingestion/watcher.py` — the folder watcher and property/state caches (A2 item); its
  `_resolve_from_path` is what would need to change (or be replaced) for Part G depending on
  the real OneDrive folder's structure.
- `ingestion/embedder.py` — semantic embeddings and the reindex path (A3 item).
- `system/pipeline/property_scraper.py` — loads the Project Master; contains the frozen built-in
  fallback list.
- `system/pipeline/property_matcher.py` — matches document/email content to a property by name rather
  than by folder path; the fallback approach for Part G if the existing OneDrive folder isn't
  organized by State/Property.
- `system/pipeline/outlook_auth.py` — Outlook/Graph sign-in flow (also the auth foundation for the
  Part F Option-B automatic transcript fetch).
- `system/pipeline/scheduler.py` — the background scheduler (where the Part F Option-B transcript poll
  would live).
- `system/analysis/rag_engine.py` — the retrieval layer all search tools call; a meeting-search tool
  (Part F) would go through here with a `type="meeting"` filter.
- `system/mcp_server.py` — registers all the tools; hosts the scheduler and watcher (a new
  `search_meetings` / meeting-retrieval tool from Part F would be registered here).
- `README.md`, `system/requirements.txt` — onboarding docs and dependencies.
