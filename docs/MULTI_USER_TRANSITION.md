# Vaulter AI — Multi-User Transition Analysis & Roadmap

**Date:** 2026-07-21
**Scope:** A grounded review of the current codebase after the recent bug-fix work, plus a
prioritized plan for moving from one user to a whole team (~5–15 non-technical real-estate
staff), each running their own fully-local instance.

This document is written to be readable by a non-technical reader. Each section ends with a
short "For implementers" note where there's a concrete technical detail worth preserving.

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

## Part A — Checking the recent work

An independent review re-verified this session's changes. It explicitly confirmed the
following are correct: the duplicate-address handling in screening, the Excel sold-property
fix, the crash-safe/atomic file writes, the email sender-whitelist logic, the removed dead
code, the manifest cache-key consistency, and the divide-by-zero guard in scoring.

Three small loose ends remain. None are urgent; all are quick.

### A1. OCR memory spike on mixed PDFs *(regression introduced this session)*
The fix that lets a mostly-digital PDF still capture its occasional scanned page currently
renders the **entire** PDF to images the moment **any** single page has no text — including a
blank separator page. On a 300-page document with one scanned page, that's a large, needless
memory and time spike.
**Fix:** render only the specific page that needs OCR, not the whole document.
*For implementers: `ingestion/extractor.py`, `_extract_pdf` — pass `first_page=page_num,
last_page=page_num` to `convert_from_path`, or render lazily per-page, instead of rendering the
whole file once.*

### A2. A brand-new *state* isn't recognized until restart
The property-list auto-refresh added this session correctly picks up new *properties* without
a restart, but adding a brand-new *state* (e.g. "Nevada") to the Project Master can cause the
first file in that state to be misfiled to `processed/unknown/` until the next file for an
already-known state, or a restart.
**Fix:** refresh the valid-states set on the same file-change signal the property list uses.
*For implementers: `ingestion/watcher.py` — `_get_valid_states()` is consulted before
`_load_properties()` refreshes; give it its own mtime check.*

### A3. Search quietly degrades after an upgrade unless `reindex` is run
Switching to real semantic search means a database created before the upgrade needs a one-time
`python main.py reindex`, or search silently returns weaker results. This is wired up and
documented, but it's manual and the failure is invisible.
**Fix:** detect the mismatch and surface a plain-English prompt (and fold it into the health
check in Part D).

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

*For implementers: `config.py` (`SECRETS_DIR`, `TESSERACT_PATH`, `POPPLER_PATH`), `README.md`
setup section, `requirements.txt` (dependencies are largely unpinned; the author's committed
environment is on a bleeding-edge Python that may not have prebuilt packages on staff
machines).*

### Theme 2 — Nobody can tell when a copy has quietly broken (highest leverage)
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

## Part C — The shared-folder concurrency issues (most technically serious)

Ordered by seriousness. The first is the one to take most seriously.

### C1. The team's saved results can be silently wiped *(most serious)*
If one person's app reads the shared results record at the exact instant OneDrive is
mid-way through syncing a new copy of it, the app can misread the half-written file as
*empty* — and then overwrite the shared record with **only its own single latest entry**,
discarding everyone else's saved (already-paid-for) results. The leftover result files become
orphans that can never be matched again, so those files get re-screened and re-paid.
**Impact: silent loss of shared data + guaranteed re-spend.**
**Fix direction:** teach the file layer to tell apart "file is genuinely empty" from "file is
present but currently unreadable," and in the unreadable case **refuse to overwrite** rather
than replacing good data with a fresh single-entry file. Small, safe, localized change.

### C2. Two people finishing at the same time — one entry lost
Two people finishing screening runs at nearly the same moment can each save their result, and
OneDrive keeps one as the official file and quietly renames the other to a "conflict copy" that
nothing ever reads. One person's completed, paid-for result vanishes from the shared record.
**Fix direction:** a small routine that, on startup, finds OneDrive conflict-copy files, merges
their contents back into the official record (safe because entries are uniquely keyed), and
deletes them.

### C3. Two people screening the same file the same morning both pay full price
The cost-saving cache only helps *after* a run has finished and synced. There's no "someone is
already working on this" marker, so if a broker emails one file to the whole team and three
people screen it that morning, all three pay in full for the Claude and Google Maps calls.
**Fix direction:** drop a short-lived "in progress" marker in the shared folder at the start of
a run; if a fresh one from someone else exists, wait for their result instead of paying again.

### C4. A cached result can point to a file that hasn't finished downloading
The record can say a result exists and point to its file before that (large) file has finished
syncing to the reader's machine — so the reader correctly skips paying, but the file they open
is incomplete.
**Fix direction:** before trusting a cached result, confirm the results file is actually fully
present and valid, not just that a placeholder exists.

### C5. Smaller items
- Result files are written directly (not atomically), so a reader can briefly catch a
  half-written file. Fix: write to a temp file and rename into place (the pattern already used
  elsewhere in the code).
- Two people screening *different* files for the same market within the same second can produce
  the identical filename. Fix: add a short unique suffix to result filenames.
- The dashboard can show a blank/broken view if it reads a file mid-sync. Fix: retry, and skip
  a single unavailable entry rather than blanking the whole view.

*For implementers: `safe_io.py` (`load_json`/`locked_json_update`), `analysis/screening/
pipeline.py` (`_update_manifest`, `_find_cached_result`, `run_full_screening`), `analysis/
screening/workbook_builder.py` (non-atomic save), `analysis/screening/dashboard_server.py` and
the dashboard HTML.*

---

## Part D — Prioritized improvement roadmap

Each item is independent and can be done on its own. Recommended order:

### Priority 0 — Tidy the loose ends from recent work *(fast)*
Fix A1, A2, A3 above. Small, self-contained, no rollout dependencies.

### Priority 1 — Health-check tool *(highest leverage)*
A read-only "is my copy working?" check covering, in plain English: Outlook sign-in status,
how much data is in the database and when it last ingested, whether the background scheduler
is running and its last error per job, whether the shared folder is really connected (vs
silently fallen back to local), which portfolio file is in effect and its date, and the
running code version. This turns every silent failure in Theme 2 and Theme 4 into something
checkable.

**Proactive, not on-demand.** For non-technical staff, a tool they have to remember to ask for
defeats the purpose — the whole problem is failures that are silent, and someone who doesn't
know to ask never finds out. Since Claude reads each MCP tool's own description when it
connects, the health-check tool's description should instruct Claude to run it automatically
at the start of every conversation, before anything else — not something the user has to
request. Two things make this work well rather than becoming a nuisance:
- **Silent when healthy.** If everything checks out, Claude says nothing about it at all and
  just proceeds with whatever the user actually asked. The check should only surface into the
  conversation when it finds something actually wrong (auth expired, scheduler dead, shared
  folder disconnected, etc.) — otherwise every single conversation would open with a "yep,
  all good!" that becomes noise nobody reads.
- **One check per session, not per message.** It should run once when a conversation starts,
  not be re-run on every message — a lightweight local check is cheap, but running it
  constantly for no reason is still unnecessary overhead and risks feeling naggy.

*For implementers: this depends on Claude actually following an instruction embedded in a tool
description rather than a hard technical guarantee (MCP doesn't have a true server-push
mechanism into an existing chat) — worth explicitly testing that Claude Desktop reliably calls
it at conversation start before relying on it as the primary safety net.*

### Priority 2 — Shared-folder safety *(protects real money and data)*
Fix C1 first (the silent-wipe), then C2 (conflict-copy merge), then C3 (in-progress marker),
then C4/C5. C1 alone removes the only data-loss risk in the whole shared-folder story.

### Priority 3 — Easy onboarding *(unblocks the actual rollout)*
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

## Part F — New capability: searchable Monday-meeting transcripts

**Goal:** record the weekly Monday meeting, and let anyone later ask Claude what was said —
"what did we decide about the Forney deal last Monday?" — and get back the actual spoken
passages. Purely to refresh memory, so nothing is forgotten.

**Decisions made during brainstorming:**
- **Capture:** meetings run on Microsoft Teams. This is the smoothest path — Teams can
  auto-record and auto-transcribe, and it fits the existing Microsoft/OneDrive setup.
- **What we want back:** a **searchable transcript** (the exact spoken passages). This is the
  simplest, reuses the existing search pipeline, and costs **no extra API money**. Richer
  options (auto summaries/action items, links back to the recording, speaker-by-speaker
  attribution) were considered and deferred — they can be layered on later.
- **Sharing:** meeting transcripts are **shared** across everyone in the meeting (like the
  screening results, unlike private email). This fits the privacy model cleanly.

### How it fits the existing system
The transcript is just text that flows into the same ingest → search → retrieve pipeline
already used for PDFs and emails. Transcripts live in a shared team folder (a sibling of the
screening folder in OneDrive); each person's own local Claude instance ingests its own
searchable copy. That keeps search fast and local while the *source* stays shared — and it's
safe to share because a transcript isn't private the way one person's inbox is.

*For implementers: do NOT try to share one ChromaDB across machines over OneDrive — a shared
SQLite-backed store synced by OneDrive will corrupt. The correct pattern is: shared transcript
**files** in OneDrive, ingested into each person's **local** ChromaDB, tagged `type="meeting"`.*

### What to do first (in order)

**Step 1 — Turn on Teams recording + transcription (no code; do this first).**
Set the recurring Monday meeting to auto-record and auto-transcribe so no one has to remember.
This is an admin/organizer setting and can happen in parallel with the code work. Until
transcripts exist, there's nothing to ingest.

**Step 2 — The simple pipeline (first code version).**
A shared "Meetings" folder in the team OneDrive. A transcript goes in; each person's local
instance ingests it (date + title as metadata, tagged as a meeting); a friendly tool answers
plain-English questions like "what did we discuss last meeting?" or "search meetings for X."
Small amount of new code; reuses the whole existing pipeline.

**Step 3 — Automate the capture (later polish).**
Have the system fetch the transcript from Teams automatically after each meeting, so no human
touches a file.

### The one real choice: how the transcript reaches the shared folder

- **Option A — Someone drops it in (manual capture, automatic everything else).** After the
  meeting, one person downloads the Teams transcript and drops it into the shared Meetings
  folder; everything downstream is automatic.
  *Pros:* works immediately, no special permissions, zero risk. *Cons:* one manual step weekly.
- **Option B — Fully automatic fetch from Teams.** The system reads the transcript from Teams
  itself (via Microsoft Graph).
  *Pros:* nobody lifts a finger. *Cons:* needs a one-time IT/admin approval to let the app read
  meeting transcripts, so more setup before it works.

**Recommendation: build Option A now, design the folder and file format so Option B slots in
later with no rework.** Immediate value; automate once admin approval is arranged.

*For implementers: Teams recordings/transcripts for a non-channel meeting save to the
organizer's OneDrive "Recordings" area; the transcript can be downloaded as `.vtt` or `.docx`.
Option B uses the Microsoft Graph online-meeting transcript endpoints, which require an
application permission (e.g. `OnlineMeetingTranscript.Read.All`) and admin consent — a heavier
lift than the file-drop path. The existing scheduler thread is the natural place to poll for
new transcripts once B is built.*

### Ideas to keep it smooth for non-technical staff
- **Make capture invisible:** auto-record the recurring meeting, and ideally point Teams to
  save straight into the folder the app already watches, so even the "drop it in" step
  disappears.
- **No filing or organizing:** date and title come from the meeting itself; users never name
  or sort anything.
- **Natural questions, not folders:** users ask Claude in plain English and never think about
  where transcripts live.
- **It just appears:** ingestion runs in the background (the watcher already does this), so a
  searched meeting shows up on its own.

### Things to design around (to head off bugs)
- **Transcript format.** Teams transcripts arrive as a subtitle-style `.vtt` file full of
  timestamps, which the current extractor doesn't handle. Two clean options: save/export the
  transcript as a Word document (already read cleanly by the pipeline via `mammoth`), or add a
  small `.vtt` parser that strips timestamps and keeps the spoken text (and speaker names, if
  attribution is on). Decide up front.
  *For implementers: `.vtt` is not in `ingestion/extractor.py`'s `SUPPORTED_EXTENSIONS`; either
  route to the existing `.docx` path or add a `.vtt` handler.*
- **Meetings don't belong to a property.** The current filing is organized by State/Property,
  and a meeting isn't tied to one property. Meetings need their own lane — a separate "meeting"
  category and ingest path — rather than being forced into the State/Property folder structure.
  *For implementers: the watcher's `_resolve_from_path` and the State/Property folder validation
  assume a property context; a meetings ingest path should bypass that and tag `type="meeting"`
  with date/title metadata instead.*
- **Shared-folder concurrency.** Multiple people *reading/ingesting* the same shared transcript
  is safe (read-only for them). Only the *write* side (dropping/fetching the transcript) has any
  concurrency concern, and that's a single designated flow — so the Part C hazards mostly don't
  apply here, but the same atomic-write discipline should be used when the automated fetch (B)
  writes into the shared folder.

### Suggested priority placement
This is independent of the multi-user workstreams in Part D and can proceed on its own timeline.
A sensible sequence: **Step 1 (Teams setting) immediately**, then the **Step 2 simple pipeline**
as a small standalone piece of work, with **Step 3 automation** grouped alongside the Part D
"easy onboarding / Graph" work since it shares the admin-consent setup.

---

## Part G — Confirmed architecture: shared portfolio documents via the existing OneDrive

**This is a settled decision, not a proposal.** It confirms and generalizes what Theme 4 (Part B)
had already flagged as a risk — that portfolio reference data was being forced into per-user
state — and supersedes the narrower "just the Project Master" framing originally in Priority 4.

### The confirmed data-flow model

| Data | Source | Destination |
|---|---|---|
| **Portfolio documents** (due diligence PDFs, financials, and the Project Master itself — everything currently dropped into `watched_folder`) | The company's **existing** OneDrive document repository — already in place today, already the team's real source for these documents | Every person's own **local, private** ChromaDB. Everyone ingests the same shared source independently. |
| **Email and anything derived from it** (attachments, etc.) | Private per person (their own Outlook) | That person's local database **only** — never shared, never visible to a colleague's instance. No change from today. |
| **Screening results** (combined workbook + analysis) | Generated by whoever runs `screen_listings` | Written back to the shared OneDrive (already the existing, working design — see `SCREENING_OUTPUT_DIR` in `config.py`) so the whole team benefits from one run instead of each person re-running it. |

The privacy boundary is unchanged and remains load-bearing: each staff member runs a complete,
independent local instance (own ChromaDB, own Outlook auth), so email never touches anyone
else's machine. What changes is that portfolio *documents* — like screening output already
does — are recognized as shared team data, not personal state, and get treated that way.

### What this means concretely
- **`data/watched_folder/` was only ever a local test stand-in**, not a production design. In
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
  local `WATCH_DIR`, using the same OneDrive-detection pattern `config.py` already has for
  `SHARED_DIR`/screening output.
- If it's organized some other way (by deal name, by year, flat, by document type) — the
  watcher's folder-parsing logic (`_resolve_from_path`) needs to adapt to however it's actually
  organized, which may mean deriving state/property some other way (e.g. matching document
  content or filename against the Project Master, closer to how `property_matcher.py` already
  works for email/web content) rather than assuming a folder-path convention that may not hold.

*For implementers: don't guess at this — the first real step of implementing Part G is looking
at the actual existing OneDrive folder tree and deciding which of the above (or some blend)
applies, before touching `config.py` or `ingestion/watcher.py`.*

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

- `config.py` — paths, shared-folder detection, OCR tool paths, the scraping on/off flag.
- `safe_io.py` — the shared file read/write layer (atomic writes, same-machine lock). The
  same-machine-only limitation is the root of the Part C hazards and is documented in its
  header.
- `analysis/screening/pipeline.py` — the shared manifest/cache logic and the cost-saving
  cache check.
- `analysis/screening/phase3_deep_analysis.py`, `phase4_verification.py` — per-listing and
  per-finalist shared caches; the Claude calls that cost money.
- `analysis/screening/workbook_builder.py` — writes the result spreadsheets (non-atomically).
- `analysis/screening/dashboard_server.py` + dashboard HTML — the shared results viewer.
- `ingestion/extractor.py` — PDF/Excel text extraction (A1 OCR item).
- `ingestion/watcher.py` — the folder watcher and property/state caches (A2 item); its
  `_resolve_from_path` is what would need to change (or be replaced) for Part G depending on
  the real OneDrive folder's structure.
- `ingestion/embedder.py` — semantic embeddings and the reindex path (A3 item).
- `pipeline/property_scraper.py` — loads the Project Master; contains the frozen built-in
  fallback list.
- `pipeline/property_matcher.py` — matches document/email content to a property by name rather
  than by folder path; the fallback approach for Part G if the existing OneDrive folder isn't
  organized by State/Property.
- `pipeline/outlook_auth.py` — Outlook/Graph sign-in flow (also the auth foundation for the
  Part F Option-B automatic transcript fetch).
- `pipeline/scheduler.py` — the background scheduler (where the Part F Option-B transcript poll
  would live).
- `analysis/rag_engine.py` — the retrieval layer all search tools call; a meeting-search tool
  (Part F) would go through here with a `type="meeting"` filter.
- `mcp_server.py` — registers all the tools; hosts the scheduler and watcher (a new
  `search_meetings` / meeting-retrieval tool from Part F would be registered here).
- `README.md`, `requirements.txt` — onboarding docs and dependencies.
