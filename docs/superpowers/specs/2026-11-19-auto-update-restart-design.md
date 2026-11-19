# Automatic updates with an automatic Claude restart — design

Date: 2026-11-19. Owner's request, in their words: "make it where user don't have to manually do
a health system check ... automatically updates", then "if there is an update, it automatically
restarts claude by itself in the background without user approval". Chosen timing: **restart once
the computer has been idle for 15 minutes** (option A of three).

## Intent

A teammate should never have to do anything for an update to reach them: no health check, no
"yes, install it", no quitting and reopening Claude Desktop. The owner publishes; within hours every
teammate's machine is running the new version.

## How it works today (what this changes)

* **Download is already automatic.** `_check_and_stage_update` runs from `check_for_update` (start
  of a chat) and from the tool wrapper on a one-hour gate. It verifies the signature and writes
  `system/data/pending_update/ready.json`.
* **Install asks.** `check_for_update` tells Claude to ask "install now?"; on yes Claude calls
  `apply_pending_update` (inside a tool call, which once timed out — CLAUDE.md, 2026-08-21).
* **Restart is manual.** The server cannot restart its own parent app, so every apply ends with
  "fully quit and reopen Claude Desktop". `_restart_pending` nags until it is done.

## Goals

1. A downloaded update installs with no question asked.
2. Claude Desktop restarts by itself to load it, only when the person has been away 15 minutes.
3. Nothing a person sees in a chat mentions updates unless something went wrong.

## Non-goals

* Changing how releases are built, signed, published or staged (canary → general stays).
* Mac support for the restart (every teammate is on Windows). Install-on-startup works anywhere.
* Updating the Copilot/Cowork plugin or the GitHub plugin marketplace.

## Anti-goals (would make this a failure even if it technically works)

* **Killing Claude Code.** Claude Code's CLI is also `claude.exe` (measured on this machine: four
  CLI processes beside the Desktop ones). Closing one ends someone's coding session. Only the
  Desktop app may ever be closed.
* **Overwriting a development copy.** A git clone has no `VERSION` file and `_published_is_newer`
  treats that as "always older" — so an auto-apply there would replace the maintainer's working
  repo with a package. TODD-LAPTOP's Claude Desktop runs the dev repo directly. Auto-install must
  never run on a folder with `.git` beside `system/`, or with no `system/VERSION`.
* **Installing a downgrade.** The same superseded-stage rules `_update_ready()` applies must gate
  the automatic path; it must ask `_update_ready()`'s rule, not re-implement a weaker copy
  ("when three functions answer one question, the rule belongs in one of them").
* **A restart loop.** If an install keeps failing, the machine must not restart Claude every 15
  minutes forever.
* **Silent failure.** A failed automatic install or restart must reach the team's error reports,
  not just a local log nobody reads.

## Approaches considered

* **A. Install at startup + restart when idle 15 min (chosen).** Fast to land, rarely interrupts.
* B. Install at startup + restart at a fixed overnight time. Never interrupts, but a laptop that is
  asleep or shut at that hour misses it until Claude is next opened.
* C. Install at startup + restart immediately. Fastest, but closes Claude mid-answer.
* Rejected outright: having the server re-launch or hot-reload itself. A re-exec on Windows starts a
  new process, so Claude Desktop sees its server die; reloading modules in place leaves old and new
  code mixed. Neither is trustworthy for a non-technical user's only connection.

## Design

### Part 1 — install at startup (`system/main.py`, `system/scripts/apply_update.py`)

When `main.py mcp` starts, **before importing `config` or anything else of ours**:

1. If `system/data/pending_update/ready.json` does not exist → carry on. (Path is fixed relative to
   `main.py`, so this costs one file check; nothing is imported.)
2. Otherwise run the install as a **separate Python process** —
   `python scripts/apply_update.py --auto` — with stdin closed and output to a file, never the MCP
   pipe (the 2026-08-21 lessons), and a timeout. A separate process means the server then imports
   only new code; nothing old is already loaded.
3. `--auto` refuses (and logs why) unless all hold: not a development copy (no `.git` next to
   `system/`, and `system/VERSION` exists); the staged version passes the same superseded-stage
   test as `_update_ready()` (move that test somewhere both can import, e.g. a small function in
   `apply_update.py` that `_update_ready()` also calls); signature verifies (already done inside
   `apply_pending_update`).
4. Then the server starts normally on the new code. Nothing is said in chat.

Cost when nothing is staged: one `exists()` check. Cost with an update: ~1 s (measured apply time),
more only when `requirements.txt` changed.

### Part 2 — restart when idle (`system/scripts/auto_restart.py`, new)

A Windows scheduled task, **"Vaulter AI - Update restart"**, runs every 15 minutes as the logged-on
user (it must run in the person's own session to read idle time and reopen their app). Each run:

1. **Is a restart owed?** Yes if an update is staged and passes the Part 1 tests, or if the version
   the running server loaded differs from `system/VERSION` (an update applied by hand). For the
   second, the server writes the version it loaded to `system/data/running_version.txt` at start.
   No → exit.
2. **Is Claude Desktop running?** Found by process path, never by name alone: an executable under
   `Program Files\WindowsApps\Claude_*` or `%LOCALAPPDATA%\AnthropicClaude`, and never under
   `.local\bin` (Claude Code). Not running → exit; the next time it opens, Part 1 installs.
3. **Has the person been away 15 minutes?** Windows' last-input time (`GetLastInputInfo`). No →
   exit; try again next run.
4. **Loop guard.** Record each restart for a version in `system/data/auto_restart.json`. At most 2
   restarts per version; past that, stop and log an `[ERROR]` so it reaches the team's error
   reports.
5. **Restart.** Stop the Desktop processes found in step 2 (the whole Desktop process tree; Claude
   Desktop hides to the tray when its window closes, so closing the window is not enough). Wait for
   them to exit. Reopen it **the same way it was installed**: a Store install through its Start
   menu entry (`explorer.exe shell:AppsFolder\<AppID>`, AppID from `Get-StartApps`, e.g.
   `Claude_pzs8sxrjxfjjc!Claude` here), a classic install by its own executable.
6. Log every decision (one line per run that did something, counts and versions only) to
   `system/data/logs/auto_restart.txt`. An `[ERROR]` line goes to `vaulter.log` on failure so
   `_report_errors_to_team` carries it.

**Who creates the task:** the server, as part of the quiet upkeep in `check_for_update`
(`_ensure_restart_task`, same pattern as `_deal_tracker_task`: values passed as environment
variables, `pythonw.exe`, output to a file). Only on Windows, only on a real install (Part 1's
dev-copy test), and only if it is missing. So it reaches every teammate with the release that
carries it, with nothing for them to do.

### Part 3 — what Claude says in chat (`system/mcp_server.py`)

* `check_for_update`: an update that is staged now returns "will install automatically; say
  nothing". A restart owed returns "say nothing" too, since the task handles it — unless the loop
  guard has given up, in which case Claude tells the person once to quit and reopen Claude.
* The per-answer notice (`_with_pending_notice`) follows the same rule.
* `apply_pending_update` stays as a manual fallback.

### Records to update

* CLAUDE.md, auto-update section: it currently says apply is "deliberately confirm-then-apply, not
  fully automatic ... given the blast radius". Record the owner's 2026-11-19 decision to make it
  automatic, and that the **canary test in the publish-release skill is now the only check before
  an update reaches everyone**.
* The publish-release skill: say plainly that publishing now restarts everyone's Claude within
  hours of their next idle period.

## Decisions after the builder check (2026-11-19, builder's calls, owner can overrule)

1. **A failed or interrupted install must not leave `system/` half old, half new.** Before syncing,
   `--auto` copies the program files it is about to replace (everything `apply_update()` would
   delete or overwrite — never data/, confidentials/ or the venv) to
   `system/data/backups/pre_update_<version>/`, and restores them if the sync raises. The marker
   `APPLY_IN_PROGRESS_FILE` already exists; at startup, an unfinished marker plus a backup folder
   means "restore first, then start". Restoring and starting on the old version beats starting on a
   mixed tree or not starting at all. An `[ERROR]` is logged so the next working conversation
   reports it.
2. **The install mostly happens while Claude Desktop is CLOSED, not while it is starting.** The
   restart task does: stop Desktop → install (no time pressure, pip allowed) → reopen. Startup
   install remains only for the case where the person opens Claude themselves, and there it is
   skipped when the release changes `requirements.txt` (the task will do it at the next idle
   restart instead), so startup never waits on pip.
3. **Two servers from one install can overlap.** Startup install takes an exclusive lock file
   (`system/data/apply.lock`, created with `O_EXCL`, stale after 10 minutes) and also skips if
   another `main.py mcp` from the same install folder is already running (process command-line
   check). The restart task runs only after Desktop and its servers are gone, so it has no overlap.
   `running_version.txt` is dropped: "restart owed" is now just "an applicable update is staged",
   or the `VERSION` file is newer than the start time of Desktop's main process.
4. **The task also checks the channel**, at most once an hour, by calling the same
   `_check_and_stage_update` the server uses (imported, not copied), so a machine that is left open
   with no Vaulter questions still updates within hours.
5. **Claude Code sessions started inside Claude Desktop count as part of Desktop.** Quitting Desktop
   by hand ends them too; the 15-minute idle rule is the protection. Standalone Claude Code
   (`.local\bin\claude.exe`, not a child of Desktop) is never touched.

## Rollout

* The release carrying this still reaches each teammate the **old way, once**: they say yes and
  restart. Every update after that is automatic.
* Test on a real install before publishing (this machine has none; build one with
  `build_handoff.py` into a scratch folder, or use a teammate machine with permission): stage a
  canary update, confirm Part 1 installs it on start, confirm the task waits while active and
  restarts after 15 idle minutes, confirm Claude Code processes are untouched, confirm the loop
  guard gives up after 2.
* Note: the working tree currently holds another session's uncommitted changes
  (`extract.py`, `tracker.py`, two check scripts). This work must be committed without them.

## Known limits (stated, not solved)

* Idle means no keyboard or mouse. If Claude Desktop is running a long task by itself while the
  person is away, the restart ends that task. Past chats are kept.
* A computer that is asleep runs nothing; the restart happens after it wakes and goes idle again,
  or the install happens when Claude is next opened.
* When Claude reopens, its window appears. The person was away, so this is acceptable.

## Left to the builder

* Exact file and function names; where the shared superseded-stage test lives.
* Process-tree stopping method and wait timeout.
* Check coverage: new checks in `check_portfolio_comparison.py` §6 (the update section) for the
  dev-copy refusal, the downgrade refusal, the Claude Code exclusion (feed it real command lines /
  paths, as `_desktop_settings_from_processes` does), the loop guard, and the chat wording.
