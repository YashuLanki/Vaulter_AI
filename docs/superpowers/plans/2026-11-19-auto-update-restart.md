# Automatic Updates and Idle Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A published Vaulter AI update installs on every teammate's machine and Claude Desktop restarts itself to load it, with no question asked and no manual step.

**Architecture:** The install rule and the guarded automatic install live in `system/scripts/apply_update.py`, the one module both other parts call. `main.py mcp` installs a staged update at startup in a separate process before importing any of our code. A new Windows-task script, `system/scripts/auto_restart.py`, checks the channel hourly and, once the computer is idle for 15 minutes, stops Claude Desktop, installs, and reopens it. `mcp_server.py` registers that task and stops mentioning updates in chat unless something failed.

**Tech Stack:** Python 3.15 (`.venv`), Windows Task Scheduler via PowerShell, `ctypes` (GetLastInputInfo), `taskkill`, `explorer.exe shell:AppsFolder`. Hand-rolled check scripts (no pytest).

**Spec:** `docs/superpowers/specs/2026-11-19-auto-update-restart-design.md`

## Global Constraints

- Work in a fresh worktree on a new branch `auto-update` cut from `deal-tracker-inbox` HEAD (`af597f1`). The main checkout holds another session's uncommitted work (`extract.py`, `tracker.py`, `check_deal_tracker.py`, `check_portfolio_comparison.py`); never touch, stage or stash it.
- Python for every command: `C:/Users/YashuLanki/vaulter_ai/.venv/Scripts/python.exe` (written `$PY` below). The worktree has no `.venv` of its own.
- New checks go in a NEW file `system/scripts/check_auto_update.py` (same `check(name, condition, detail="")` style as `check_portfolio_comparison.py`), so this branch does not collide with the other session's edits to that file. Every check uses temp folders; nothing may write to the shared OneDrive folder or to this machine's real `system/data/pending_update`.
- Never close Claude Code: standalone `claude.exe` under `.local\bin` is never stopped.
- Never auto-install on a development copy: `.git` (file or folder) beside `system/`, or no `system/VERSION`.
- Anything inside the MCP server that runs a subprocess: `stdin=subprocess.DEVNULL`, output to a file, never pipes (CLAUDE.md 2026-08-21).
- Idle threshold 15 minutes (900 s). Task runs every 15 minutes. Channel check at most hourly. At most 2 restarts per version. Lock stale after 600 s. Task name exactly `Vaulter AI - Update restart`.
- Plain-language log lines; the auto-restart log holds counts and versions only, never file or property names.
- Real-name leak hook stays green; no real names or figures in code, comments or CLAUDE.md.

## Review Focus

1. **Claude Code running beside Desktop** — the restart must stop only Desktop's processes. Test: Task 3 feeds the real command lines seen on this machine (four `.local\bin\claude.exe`, nine `WindowsApps\Claude_*\app\claude.exe`) and asserts only the Desktop root is chosen.
2. **A dev copy with an update staged** (the maintainer's own laptop, whose Claude Desktop runs the repo) — must refuse at startup and in the task. Test: Task 1 asserts `auto_apply` refuses with `.git` present and with `VERSION` missing; Task 4 asserts the task is not registered on a dev copy.
3. **A crash mid-sync** — files half replaced. Test: Task 1 makes the sync raise after the first file and asserts every original file is back byte-for-byte.
4. **An update that fails every time** — must not restart Claude forever. Test: Task 3 asserts the third attempt for one version is refused and writes one `[ERROR]` line, not one per run.
5. **Two servers starting at once** — only one may install. Test: Task 1 asserts a second `auto_apply` while the lock is held refuses, and a lock older than 600 s is taken over.

---

### Task 1: The guarded automatic install (`apply_update.py`)

**Files:**
- Modify: `system/scripts/apply_update.py`
- Modify: `system/mcp_server.py:6254-6306` (`_update_ready` calls the shared rule)
- Create: `system/scripts/check_auto_update.py`

**Interfaces:**
- Produces (all in `apply_update.py`):
  - `is_development_copy(project_root: Path) -> bool` — True if `project_root.parent / ".git"` exists or `project_root / "VERSION"` does not.
  - `running_version(project_root: Path) -> str` — first line of `VERSION`, "" if absent.
  - `staged_update_applicable(pending: dict | None, running: str) -> str` — the version to install, or "". "" when pending is not a dict, has no version, version == running, or `current_version_at_download` is set, `running` is non-empty, and they differ. (Exactly `_update_ready`'s current rule, moved here.)
  - `release_changes_dependencies(project_root: Path, zip_path: Path) -> bool` — True if the zip's `requirements.txt` bytes differ from the one on disk (or either cannot be read).
  - `auto_apply(project_root: Path | None = None, *, at_startup: bool = False) -> dict` — returns `{"applied": bool, "version": str, "reason": str, "restored": bool}`.
  - `recover_unfinished_apply(project_root: Path | None = None) -> bool` — True if it restored a backup.
  - `other_servers_running(project_root: Path) -> bool` and the pure helper `_count_servers(procs: list[tuple[int, str]], main_py: Path, self_pid: int) -> int`.
  - CLI: `python apply_update.py --auto [--startup]` runs `recover_unfinished_apply()` then `auto_apply(at_startup=...)`, prints one result line, exit code 0 always.
- Consumes: existing `apply_pending_update()`, `apply_update()`, `config.PENDING_UPDATE_DIR`, `config.APPLY_IN_PROGRESS_FILE`, `config.DATA_DIR`.

`auto_apply` order: dev-copy refusal → load pending → `staged_update_applicable` → if `at_startup` and (`release_changes_dependencies` or `other_servers_running`) refuse with a reason saying the idle restart will install it → take lock `DATA_DIR/apply.lock` (`os.O_CREAT|os.O_EXCL`; a lock older than 600 s is removed and retaken) → back up to `DATA_DIR/backups/pre_update_<version>/` every file under `project_root` not under `PRESERVED_DIR_NAMES` (and not under `data/`) → `apply_pending_update(project_root)` → on any exception restore the backup (sync it back: delete files not in the backup, copy backup files in), log `[ERROR]` via `logging.getLogger("vaulter.update")`, return `restored=True` → on success delete the backup → always release the lock. `recover_unfinished_apply`: if `APPLY_IN_PROGRESS_FILE` exists and a `pre_update_<its version>` backup exists, restore it, log `[ERROR] an update to <version> did not finish; the previous version was put back`, and leave the marker for `_report_unfinished_apply` to report.

Refactor `apply_update()` so the delete-then-copy sync is `_sync_tree(project_root: Path, src_dir: Path) -> tuple[int, int]`, used by both apply and restore.

- [ ] **Step 1: Create the worktree** — `git worktree add .claude/worktrees/auto-update -b auto-update deal-tracker-inbox`; all later paths are inside it.
- [ ] **Step 2: Write failing checks in `check_auto_update.py` section "1. The automatic install"**, each on a temp `system/` tree with a `VERSION` file and a hand-built zip + `ready.json` (signature check stubbed by monkeypatching `apply_update.apply_pending_update` where the check is about gating, and real `apply_update()` where it is about files):
  - `is_development_copy` True with a `.git` file beside, True with no `VERSION`, False otherwise.
  - `staged_update_applicable`: `{"version":"b"}`,"a" → "b"; same version → ""; `{"version":"b","current_version_at_download":"x"}`,"a" → ""; `[1]`,"a" → "".
  - `auto_apply` on a dev copy → `applied` False, reason mentions "development copy", stub never called.
  - `at_startup=True` with a zip whose `requirements.txt` differs → refused, reason mentions "restart"; same with `at_startup=False` → applied.
  - Lock held (fresh file) → refused; lock file mtime set 601 s back → applied.
  - Stubbed apply that writes one new file then raises → `restored` True and every original file identical (compare sha256 of each), the new file gone.
  - `recover_unfinished_apply` with marker + backup → returns True and files restored; marker still present.
  - `_count_servers([(1, 'python C:\\x\\system\\main.py mcp'), (2, 'python C:\\x\\system\\main.py mcp'), (3, 'python C:\\y\\system\\main.py mcp')], Path('C:/x/system/main.py'), self_pid=1)` == 1.
- [ ] **Step 3: Run** `$PY system/scripts/check_auto_update.py` — expect FAIL lines (functions missing).
- [ ] **Step 4: Implement the interfaces above**; `other_servers_running` lists processes with PowerShell `Get-CimInstance Win32_Process -Filter "Name like 'python%'"` (CommandLine, ProcessId) as JSON, output to a temp file, 20 s timeout, False on any failure or non-Windows.
- [ ] **Step 5: Point `_update_ready()` at `apply_update.staged_update_applicable(staged, _get_code_version())`**, keeping its log line for the superseded case.
- [ ] **Step 6: Run** `$PY system/scripts/check_auto_update.py` (all PASS) and `$PY system/scripts/check_portfolio_comparison.py` (section 6 unchanged: all PASS).
- [ ] **Step 7: Commit** — `git add system/scripts/apply_update.py system/scripts/check_auto_update.py system/mcp_server.py` ; message "Auto-update: guarded automatic install with backup, lock and dev-copy refusal".

### Task 2: Install at startup (`main.py`)

**Files:**
- Modify: `system/main.py:21-46` (before `from config import LOG_DIR`)
- Modify: `system/scripts/check_auto_update.py`

**Interfaces:**
- Consumes: the `--auto --startup` CLI from Task 1.
- Produces: `_auto_install_at_startup(system_dir: Path) -> None` in `main.py`. Runs only when `sys.argv[1] == "mcp"` and `system_dir/"data"/"pending_update"/"ready.json"` or `system_dir/"data"/"apply_in_progress.json"` exists. Runs `[sys.executable, system_dir/"scripts"/"apply_update.py", "--auto", "--startup"]`, `stdin=DEVNULL`, stdout+stderr to `system_dir/"data"/"logs"/"auto_install.txt"` (append), timeout 120 s, `cwd=system_dir`. Never raises; never writes to stdout.

- [ ] **Step 1: Write failing checks, section "2. Install at startup"**: with no `ready.json` in a temp `system_dir`, `subprocess.run` (monkeypatched) is never called; with one, it is called once with `--auto` and `--startup` and `stdin` DEVNULL; a raising `subprocess.run` does not propagate. Import `main` safely by loading `_auto_install_at_startup` with `importlib` from source without running module top level (extract via `ast` + `exec` of that one function, as the existing checks do with `inspect.getsource`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement and call it** right after the console-encoding block, guarded by `len(sys.argv) > 1 and sys.argv[1] == "mcp"`.
- [ ] **Step 4: Run** check_auto_update (PASS); then `$PY system/main.py stats` still works.
- [ ] **Step 5: Commit** — "Auto-update: install a staged update when Vaulter AI starts".

### Task 3: The idle restart script (`auto_restart.py`)

**Files:**
- Create: `system/scripts/auto_restart.py`
- Modify: `system/scripts/check_auto_update.py`

**Interfaces:**
- Consumes: `apply_update.auto_apply`, `staged_update_applicable`, `running_version`, `is_development_copy`; `mcp_server._update_check_due`, `_mark_update_checked`, `_check_and_stage_update` (imported lazily, only on the hourly path).
- Produces (pure helpers, all tested):
  - `desktop_root(procs: list[dict]) -> dict | None` — `procs` items have `ProcessId`, `ParentProcessId`, `ExecutablePath`, `CommandLine`, `CreationDate` (ISO str). A Desktop process has an executable under `\WindowsApps\Claude_` or `\AnthropicClaude\` (case-insensitive) and never under `\.local\bin\`. Root = the Desktop process whose parent is not a Desktop process.
  - `relaunch_command(exe_path: str, start_apps: list[dict]) -> list[str]` — Store path (`WindowsApps`) → `["explorer.exe", "shell:AppsFolder\\" + AppID]` where AppID is the `Get-StartApps` entry named exactly `Claude` whose AppID starts with the package family taken from the path (`Claude_<publisher>`); classic → `[exe_path]`; [] if neither works.
  - `restart_owed(staged: dict | None, running: str, version_mtime: float, desktop_started: float) -> str` — the version owed: the applicable staged version, else `running` if `version_mtime > desktop_started`, else "".
  - `may_restart(state: dict, version: str, limit: int = 2) -> bool` and `record_restart(state: dict, version: str) -> dict`.
  - `idle_seconds() -> float` — ctypes `GetLastInputInfo` and `GetTickCount64`; 0.0 on failure (failure means "not idle").
  - `main(argv: list[str]) -> int` with flags `--dry-run` (log what it would do, change nothing) and `--ignore-idle` (for the manual test only).
- State file `system/data/auto_restart.json`: `{"<version>": {"restarts": n, "gave_up": bool}}`. Log file `system/data/logs/auto_restart.txt`, one line per run that acted or refused for a reason other than "nothing owed". On giving up, append one `[ERROR]` line to `LOG_DIR/vaulter.log` in its existing format (`%(asctime)s  [ERROR]  ...`) once per version.

`main` order: refuse on dev copy → hourly channel check → `restart_owed` (Desktop start time from the root's `CreationDate`) → no Desktop root → exit → `idle_seconds() < 900` → exit → `may_restart` false → give-up handling → `taskkill /PID <root> /T /F` → wait up to 30 s until no Desktop process remains → `auto_apply(at_startup=False)` → relaunch with `relaunch_command` (Get-StartApps read via PowerShell before stopping) → `record_restart` → log.

- [ ] **Step 1: Write failing checks, section "3. Idle restart"**, using the process rows recorded on this machine on 2026-11-19 (paths with `C:\Users\x` substituted): `desktop_root` picks the `WindowsApps` parent (id 34004 shape) and never a `.local\bin` one; with only CLI processes → None. `relaunch_command` Store path + `[{"Name":"Claude","AppID":"Claude_pzs8sxrjxfjjc!Claude"}]` → `["explorer.exe","shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude"]`; classic `...\AnthropicClaude\claude.exe` → `[that path]`. `restart_owed` covers staged-applicable, manual-apply (mtime after start), and neither. `may_restart` true at 0 and 1, false at 2. Give-up path run twice with a stubbed environment writes exactly one `[ERROR]` line. `main(["--dry-run"])` on a dev-copy temp tree returns 0 and stops nothing (taskkill stub never called).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** All subprocess calls: `stdin=DEVNULL`, output to a temp file, timeouts.
- [ ] **Step 4: Run** check_auto_update (PASS). Then a live dry run on this machine: `$PY system/scripts/auto_restart.py --dry-run --ignore-idle` from the worktree must log "development copy, not restarting" and change nothing.
- [ ] **Step 5: Commit** — "Auto-update: restart Claude Desktop when idle to load a waiting update".

### Task 4: Register the task and quiet the chat (`mcp_server.py`)

**Files:**
- Modify: `system/mcp_server.py` — `check_for_update` (2287-2336), `_with_pending_notice` (6412-6522), new `_ensure_restart_task`, `_restart_task`, `_auto_update_active`, `_auto_restart_gave_up`
- Modify: `system/scripts/check_auto_update.py`

**Interfaces:**
- Consumes: `apply_update.is_development_copy`; state file from Task 3.
- Produces:
  - `_auto_update_active() -> bool` — Windows and not a development copy.
  - `_auto_restart_gave_up() -> str` — the version the task gave up on, or "".
  - `_restart_task(action: str) -> str` — "exists" / "add" / "remove", modelled on `_deal_tracker_task`: `pythonw.exe` runner, `auto_restart.py`, trigger `-Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 15)`, settings `-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries`, values via environment variables, output to `LOG_DIR/auto_restart_schedule.txt`.
  - `_ensure_restart_task() -> None` — once per process (module flag); only if `_auto_update_active()`; adds the task if `_restart_task("exists")` is not "". Added to `check_for_update`'s upkeep loop and called from `_with_pending_notice` beside `_write_install_checkin`.
- Chat rule, only when `_auto_update_active()`: an update ready or a restart owed → `check_for_update` returns "Up to date (…). Say nothing about this." and the notice adds nothing; if `_auto_restart_gave_up()` → one plain line asking the person to quit Claude fully (right-click its icon by the clock, Quit) and reopen it. When not active (dev copy, non-Windows) the current wording is unchanged, so `check_portfolio_comparison.py` §6 still passes.

- [ ] **Step 1: Write failing checks, section "4. What the chat says"**: with `_auto_update_active` forced True and a planted applicable `ready.json` (temp `PENDING_UPDATE_DIR`), `_with_pending_notice("x", body) == body` and `check_for_update` text contains "Say nothing"; with a gave-up state, the notice contains "Quit"; with `_auto_update_active` False, the old "ready to install" wording returns. `_ensure_restart_task` with `_auto_update_active` False never calls `_restart_task`; with True and "exists" → "not found", calls "add" once even when invoked twice.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** check_auto_update, check_portfolio_comparison, check_answers, `check_screener.py` with its usual export (all PASS), and `$PY system/scripts/check_mcp_health.py` (server starts).
- [ ] **Step 5: Commit** — "Auto-update: set up the restart task and stop asking about updates in chat".

### Task 5: Records

**Files:**
- Modify: `CLAUDE.md` (auto-update section and the MCP server notice paragraphs), `.claude/skills/publish-release/SKILL.md`, `.gitignore`, `docs/superpowers/specs/2026-11-19-auto-update-restart-design.md` (status line)

- [ ] **Step 1:** CLAUDE.md: replace "deliberately confirm-then-apply, not fully automatic" with the owner's 2026-11-19 decision, how the three parts work, the five builder-check decisions, and that the canary test is now the only gate before everyone. Note that the release carrying this reaches each teammate the old way, once.
- [ ] **Step 2:** publish-release skill: one paragraph under "Before you start" — publishing now installs on every machine and restarts Claude Desktop after 15 idle minutes, with nobody asked.
- [ ] **Step 3:** `.gitignore`: add `system/data/deal_tracker_schedule_check.txt`, `system/data/auto_restart.json`, `system/data/apply.lock`.
- [ ] **Step 4:** Copy the spec and this plan from the main checkout (they are untracked there) into the worktree's `docs/superpowers/`, then delete the main-checkout copies so they are not left behind.
- [ ] **Step 5:** Run all four suites once more; run the leak hook over the diff (`git diff deal-tracker-inbox --stat` then commit triggers it).
- [ ] **Step 6: Commit** — "Auto-update: record the decision and how it works".

### Task 6: Real-machine test (owner present)

No code. Needs the owner's go-ahead because it closes and reopens their Claude Desktop once. **Never publish a test release to the team's real update channel**: the scratch install's channel folder must be a temp folder (override `UPDATES_DIR` for the scratch install only; if config offers no override, stage the test package by writing `ready.json` + the signed zip into the scratch install's own `system/data/pending_update/` with `release.py`'s signing function instead of publishing).

- [ ] **Step 1:** Build a scratch live install with `$PY system/scripts/build_handoff.py` into a temp folder (it carries `VERSION`), point a copy of the release channel at a throwaway folder, publish a test release to it with `release.py` against that channel.
- [ ] **Step 2:** Start that install's server once with a staged update (`$PY <scratch>/system/main.py mcp` fed EOF): `auto_install.txt` shows it applied; `VERSION` changed; staging empty.
- [ ] **Step 3:** With the owner's OK, run `<scratch>/system/scripts/auto_restart.py --ignore-idle` with a staged update: Claude Desktop closes and reopens, the four standalone Claude Code processes keep their process IDs, `auto_restart.txt` records it.
- [ ] **Step 4:** Report results to the owner; only then hand over for publishing via the publish-release skill (canary first).
