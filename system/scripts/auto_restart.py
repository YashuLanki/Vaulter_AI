"""
auto_restart.py
---------------
Restart Claude Desktop when an update is owed and the person has been away for
15 minutes, so a new version takes effect with nothing for them to do.

Run every 15 minutes by the Windows scheduled task "Vaulter AI - Update restart
(<user>)" (pythonw.exe, as the logged-on user, so there is no console: nothing
here depends on print). Design: docs/superpowers/specs/2026-11-19-auto-update-restart-design.md,
"Part 2" and "Decisions after the builder check".

    python scripts/auto_restart.py [--dry-run] [--ignore-idle]

  --dry-run      decide and log what it would do; change nothing anywhere
                 (no channel check, no stop, no install, no reopen, no state write)
  --ignore-idle  skip the 15-minute idle rule (for a manual test only)

Each run, in this order, and everything that can refuse is decided BEFORE
Claude Desktop is stopped:
  1. a development copy is never touched;
  2. ask the release channel, at most hourly (the server's own functions);
  3. is a restart owed? an applicable staged update, or a VERSION file newer
     than the start of Desktop's main process (an update applied by hand) that
     was not installed at startup (data/startup_installed.json);
  4. is Claude Desktop running? found by executable path, never by name: the
     Claude Code CLI is also claude.exe, under .local\\bin, and is NEVER stopped.
     Its main process is the one Desktop process with no Electron --type=; none
     or several such, and nothing is restarted;
  5. a give-up for this version is forgotten once Desktop has been started
     (by hand) after it was recorded;
  6. has the computer been idle 15 minutes?
  7. loop guard: at most 2 restarts per version, then one [ERROR] for the team;
  8. is there a way to reopen it? if not, it is not closed;
  9. list the processes again and stop only if the same process (pid AND start
     time) is still Desktop's; count the restart, stop Desktop's process tree,
     wait (30 s of wall time) for THAT tree to go (an orphaned helper from an
     older Desktop is not waited for), install (after recovering any
     unfinished install), reopen it the way it was installed, and confirm it
     came back (60 s, one more try, then an [ERROR]).

Hardening pass (2026-11-19, check_auto_update.py section 6): a staged package
that is missing or unusable never restarts Claude (it is thrown away, so the
channel check fetches it again), nor does a version set aside after failing to
install twice; the first run after the computer wakes from sleep does not count
the idle time from before the sleep; if the person opens Claude while it is
closed for an install, nothing is installed under it; the same unexpected error
reaches the team once a day, not every 15 minutes.

Every path derives from the `system` folder (overridable for checks):
data/auto_restart.json (state), data/logs/auto_restart.txt (one line per run
that acted or refused for a reason other than "nothing owed"), and
data/logs/vaulter.log ([ERROR] lines the team's error reports pick up).
Log lines carry counts and versions only.
"""

import contextlib
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parent
for _p in (str(SCRIPTS), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import apply_update as au  # noqa: E402

IDLE_SECONDS_NEEDED = 900
RESTART_LIMIT = 2
EXIT_WAIT_SECONDS = 30
REOPEN_WAIT_SECONDS = 60             # how long a reopened Desktop has to show up, per try
REOPENED_RECHECK_SECONDS = 5         # a new Desktop must still be there this much later
WOKE_SLEEP_SECONDS = 60              # asleep longer than this since the last run = "just woke"
LOG_FORMAT = "%(asctime)s  [%(levelname)s]  %(message)s"   # what the error reports scan for
_LOG_MAX_BYTES = 256 * 1024
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # pythonw must not flash consoles


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _norm(path) -> str:
    return str(path or "").replace("/", "\\").lower()


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def is_desktop_exe(path) -> bool:
    """Claude Desktop's own executable: a Store install under WindowsApps\\Claude_*
    or a classic one under AnthropicClaude\\. Never anything under .local\\bin,
    where the Claude Code CLI lives under the same file name."""
    p = _norm(path)
    if not p or "\\.local\\bin\\" in p:
        return False
    return "\\windowsapps\\claude_" in p or "\\anthropicclaude\\" in p


def desktop_processes(procs) -> list:
    return [p for p in (procs or []) if isinstance(p, dict) and is_desktop_exe(p.get("ExecutablePath"))]


def _iso_to_epoch(text):
    """Seconds since 1970 for an ISO timestamp (PowerShell's 'o' format has seven
    fractional digits; trimmed to six). None if it cannot be read."""
    m = re.match(r"^\s*(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?\s*$", str(text or ""))
    if not m:
        return None
    base, frac, tz = m.groups()
    s = base + ("." + frac[:6].ljust(6, "0") if frac else "")
    if tz:
        s += "+00:00" if tz == "Z" else tz
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def desktop_mains(procs) -> list:
    """Desktop processes whose command line carries no `--type=`: Electron starts
    every helper (renderer, gpu-process, utility, crashpad-handler) with one, and
    the main process without."""
    return [p for p in desktop_processes(procs)
            if "--type=" not in str(p.get("CommandLine") or "").lower()]


def desktop_root(procs: list) -> dict | None:
    """The main Claude Desktop process, or None when there is not exactly one.
    Stopping its tree stops Desktop and everything it started (its servers, and
    Claude Code sessions opened inside it) and nothing else. Parentage is not
    used: an orphaned helper outliving an old main process has no Desktop parent
    either, and would be taken for the root (wrong start time, wrong process)."""
    mains = desktop_mains(procs)
    return mains[0] if len(mains) == 1 else None


def relaunch_command(exe_path: str, start_apps: list, exists=os.path.exists) -> list:
    """How to reopen Desktop the way it was installed. A Store install through its
    Start menu entry (the one named exactly "Claude" whose AppID belongs to the
    package family in the path, Claude_<publisher>); a classic install through
    its stable launcher (AnthropicClaude\\claude.exe, which starts the newest
    app-<version> folder, so a Desktop that updated itself while closed still
    opens), else its own executable; [] when neither is known, in which case
    Desktop is not closed."""
    if not is_desktop_exe(exe_path):
        return []
    p = _norm(exe_path)
    if "\\windowsapps\\" in p:
        m = re.search(r"\\windowsapps\\([^\\_]+)_[^\\]*_([a-z0-9]+)\\", p)
        if not m:
            return []
        family = f"{m.group(1)}_{m.group(2)}!"
        for app in start_apps or []:
            if not isinstance(app, dict):
                continue
            app_id = str(app.get("AppID") or "")
            if app.get("Name") == "Claude" and app_id.lower().startswith(family):
                return ["explorer.exe", "shell:AppsFolder\\" + app_id]
        return []
    if "\\anthropicclaude\\" in p:
        cut = p.index("\\anthropicclaude\\") + len("\\anthropicclaude\\")
        stub = str(exe_path).replace("/", "\\")[:cut] + "claude.exe"
        if _norm(stub) != p and exists(stub):
            return [stub]
        return [exe_path]
    return []


def restart_owed(staged: dict | None, running: str, version_mtime: float, desktop_started: float,
                 startup_installed: str = "") -> str:
    """The version a restart would load: an applicable staged update, else the
    running VERSION if that file changed after Desktop started, else "". A
    VERSION written by the startup install (startup_installed names it) is
    newer than Desktop's start too, but the server loaded it already."""
    version = au.staged_update_applicable(staged, running)
    if version:
        return version
    if running and version_mtime > desktop_started and running != startup_installed:
        return running
    return ""


def _restarts(state: dict, version: str):
    entry = state.get(version) if isinstance(state, dict) else None
    return entry if isinstance(entry, dict) else {}


def may_restart(state: dict, version: str, limit: int = RESTART_LIMIT) -> bool:
    entry = _restarts(state, version)
    if entry.get("gave_up"):
        return False
    try:
        n = int(entry.get("restarts", 0) or 0)
    except (TypeError, ValueError):
        return False      # an unreadable count must not allow a loop
    return 0 <= n < limit  # nor a negative one (only a hand edit makes one)


def record_restart(state: dict, version: str) -> dict:
    """A new state with one more restart counted for version."""
    new = {k: dict(v) if isinstance(v, dict) else v for k, v in (state or {}).items()}
    entry = _restarts(new, version)
    try:
        n = int(entry.get("restarts", 0) or 0)
    except (TypeError, ValueError):
        n = RESTART_LIMIT
    if n < 0:
        n = RESTART_LIMIT
    new[version] = {"restarts": n + 1, "gave_up": bool(entry.get("gave_up", False))}
    if "gave_up_at" in entry:
        new[version]["gave_up_at"] = entry["gave_up_at"]
    return new


def _gave_up(state: dict, version: str, now: float) -> dict:
    """A new state with version given up at `now` (epoch seconds)."""
    new = {k: dict(v) if isinstance(v, dict) else v for k, v in (state or {}).items()}
    entry = _restarts(new, version)
    new[version] = {"restarts": entry.get("restarts", 0), "gave_up": True, "gave_up_at": now}
    return new


def restarted_since_give_up(state: dict, version: str, desktop_started) -> bool:
    """True when version was given up on and Desktop's current main process
    started later than that: the person restarted it by hand, so the automatic
    restarts may try again. A give-up with no recorded time never resets."""
    entry = _restarts(state, version)
    at = entry.get("gave_up_at")
    if not entry.get("gave_up") or desktop_started is None or isinstance(at, bool) \
            or not isinstance(at, (int, float)):
        return False
    return desktop_started > at


def _reset_give_up(state: dict, version: str) -> dict:
    new = {k: dict(v) if isinstance(v, dict) else v for k, v in (state or {}).items()}
    new[version] = {"restarts": 0, "gave_up": False}
    return new


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input in this session. 0.0 on any
    failure, off Windows, or in session 0 (a service session has no person in it,
    so its idle time says nothing): failure means "not idle"."""
    if sys.platform != "win32":
        return 0.0
    try:
        k32 = ctypes.WinDLL("kernel32")
        u32 = ctypes.WinDLL("user32")
        sid = ctypes.c_ulong(0)
        if not k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)) or sid.value == 0:
            return 0.0

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not u32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        return idle_from_ticks(k32.GetTickCount64(), info.dwTime)
    except Exception:
        return 0.0


def idle_from_ticks(now_ms: int, last_input_ms: int) -> float:
    """Seconds from the last input to now. last_input_ms is GetLastInputInfo's
    32-bit tick count, which wraps every 49.7 days; now_ms the 64-bit count."""
    return float(((int(now_ms) & 0xFFFFFFFF) - int(last_input_ms)) & 0xFFFFFFFF) / 1000.0


def slept_seconds():
    """Seconds this computer has spent asleep or hibernating since it started:
    the tick count (which idle time is measured in) includes sleep, the unbiased
    interrupt time does not. None off Windows or on any failure."""
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32")
        k32.GetTickCount64.restype = ctypes.c_ulonglong
        ticks = k32.GetTickCount64()
        unbiased = ctypes.c_ulonglong(0)
        if not k32.QueryUnbiasedInterruptTime(ctypes.byref(unbiased)):
            return None
        return max(0.0, ticks / 1000.0 - unbiased.value / 1e7)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# External effects (each replaceable through main's `ops`)
# ---------------------------------------------------------------------------

def _temp_out(tag: str) -> Path:
    return Path(tempfile.gettempdir()) / f"vaulter_restart_{tag}_{os.getpid()}_{time.time_ns()}.txt"


def _powershell_json(script: str, timeout: int = 30):
    """Rows from a PowerShell `ConvertTo-Json`, or None on any failure. Output goes
    to a file, never a pipe (see CLAUDE.md, the 2026-08-21 apply lesson)."""
    out = _temp_out("ps")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            result = subprocess.run(
                [au.powershell_exe(), "-NoProfile", "-NonInteractive", "-Command",
                 "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.DEVNULL,
                timeout=timeout, creationflags=_NO_WINDOW,
            )
        if result.returncode != 0:
            return None
        raw = out.read_text(encoding="utf-8-sig", errors="replace").strip()
        data = json.loads(raw) if raw else []
        if isinstance(data, dict):
            data = [data]
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else None
    except Exception:
        return None
    finally:
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)


def _own_session_id():
    if sys.platform != "win32":
        return None
    try:
        k32 = ctypes.WinDLL("kernel32")
        sid = ctypes.c_ulong(0)
        if k32.ProcessIdToSessionId(k32.GetCurrentProcessId(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:
        pass
    return None


def _list_processes():
    """Every claude.exe in this person's own session, or None if unreadable."""
    rows = _powershell_json(
        "Get-CimInstance Win32_Process -Filter \"Name='claude.exe'\" | "
        "Select-Object ProcessId, ParentProcessId, ExecutablePath, CommandLine, SessionId, "
        "@{n='CreationDate';e={if ($_.CreationDate) { $_.CreationDate.ToString('o') } else { '' }}} | "
        "ConvertTo-Json -Compress")
    if rows is None:
        return None
    sid = _own_session_id()
    if sid is not None:
        rows = [r for r in rows if r.get("SessionId") in (None, sid)]
    return rows


def _start_apps():
    return _powershell_json(
        "Get-StartApps | Where-Object { $_.Name -eq 'Claude' } | "
        "Select-Object Name, AppID | ConvertTo-Json -Compress") or []


def _taskkill(pid: int) -> int:
    """Stop one process and its whole tree. Return code, or -1 if it could not run."""
    out = _temp_out("kill")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            result = subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                timeout=30, creationflags=_NO_WINDOW,
            )
        return result.returncode
    except Exception:
        return -1
    finally:
        with contextlib.suppress(OSError):
            out.unlink(missing_ok=True)


def _relaunch(cmd: list) -> bool:
    """Start Desktop detached, so it outlives this script and its scheduled task.
    Not waited on (it is the app). Output to DEVNULL: a temp file held open by the
    app for its whole life would never be cleaned up."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    for extra in (getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0), 0):
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=flags | extra, close_fds=True)
            return True
        except OSError:
            continue
    return False


def _channel_check() -> None:
    """Ask the release channel at most hourly, exactly as the server's tool wrapper
    does: the server's own gate, stamp and staging, imported only on this path."""
    import mcp_server
    if mcp_server._update_check_due():
        mcp_server._mark_update_checked()
        mcp_server._check_and_stage_update()


DEFAULT_OPS = {
    "list_processes": _list_processes,
    "start_apps": _start_apps,
    "taskkill": _taskkill,
    "relaunch": _relaunch,
    "idle_seconds": idle_seconds,
    "slept_seconds": slept_seconds,
    "channel_check": _channel_check,
    "recover": au.recover_unfinished_apply,
    "auto_apply": au.auto_apply,
    "sleep": time.sleep,
    "monotonic": time.monotonic,
    "time": time.time,
}


# ---------------------------------------------------------------------------
# Logs and state
# ---------------------------------------------------------------------------

def _state_path(root: Path) -> Path:
    return root / "data" / "auto_restart.json"


def _read_state(root: Path) -> dict:
    try:
        data = json.loads(_state_path(root).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _startup_installed(root: Path) -> str:
    """The version apply_update last installed at startup, or ""."""
    try:
        data = json.loads((root / "data" / "startup_installed.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def _write_state(root: Path, state: dict) -> None:
    """Raises on failure: a restart that cannot be counted must not happen."""
    path = _state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _log_line(root: Path, message: str) -> None:
    try:
        path = root / "data" / "logs" / "auto_restart.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > _LOG_MAX_BYTES:
            tail = path.read_bytes()[-_LOG_MAX_BYTES // 2:]
            path.write_bytes(tail[tail.find(b"\n") + 1:])
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass
    try:
        if sys.stdout is not None and sys.stdout.isatty():
            print(message)
    except Exception:
        pass


def _error(root: Path, message: str) -> None:
    """One [ERROR] line in vaulter.log, in the server's exact format."""
    try:
        path = root / "data" / "logs" / "vaulter.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = logging.LogRecord("vaulter.restart", logging.ERROR, __file__, 0, message, None, None)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(logging.Formatter(LOG_FORMAT).format(record) + "\n")
    except OSError:
        pass


def _error_daily(root: Path, key: str, message: str) -> None:
    """_error, at most once a day per key: a fault that repeats every 15 minutes
    (WMI broken, a disk full) would otherwise fill the team's error reports."""
    path = root / "data" / "logs" / "auto_restart_errors.json"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
        seen = seen if isinstance(seen, dict) else {}
    except Exception:
        seen = {}
    if seen.get(key) == today:
        return
    _error(root, message)
    seen[key] = today
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen), encoding="utf-8")
    except OSError:
        pass


def _just_woke(root: Path, ops: dict, dry: bool) -> bool:
    """True when the computer slept since the previous run that got this far.
    Idle time is counted in ticks, which run on through sleep, so a laptop opened
    after a night shut reads as idle for hours at the very moment its owner sits
    down. Such a run waits for the next one. Records the current figure (never in
    a dry run). False whenever it cannot tell."""
    try:
        slept = ops["slept_seconds"]()
    except Exception:
        return False
    if slept is None:
        return False
    path = root / "data" / "auto_restart_clock.json"
    try:
        prev = float(json.loads(path.read_text(encoding="utf-8")).get("slept"))
    except Exception:
        prev = None
    if not dry:
        try:
            path.write_text(json.dumps({"slept": slept}), encoding="utf-8")
        except OSError:
            pass
    return prev is not None and float(slept) - prev > WOKE_SLEEP_SECONDS


def _key(proc: dict) -> tuple:
    return _int(proc.get("ProcessId")), str(proc.get("CreationDate") or "")


def tree_keys(procs: list, root: dict) -> set:
    """(pid, start time) of root and every process descended from it in procs. A
    'child' that started before its parent is an orphan whose dead parent's pid
    was reused, and is not part of the tree."""
    by_parent = {}
    for p in procs or []:
        if isinstance(p, dict):
            by_parent.setdefault(_int(p.get("ParentProcessId")), []).append(p)
    keys, todo = set(), [root]
    while todo:
        cur = todo.pop()
        k = _key(cur)
        if k in keys:
            continue
        keys.add(k)
        started = _iso_to_epoch(cur.get("CreationDate"))
        for child in by_parent.get(k[0], []):
            c_started = _iso_to_epoch(child.get("CreationDate"))
            if started is None or c_started is None or c_started >= started:
                todo.append(child)
    return keys


def _reopen(ops: dict, cmd: list):
    """Start Desktop and confirm it shows up: True / False, or None when the
    process list could not be read at all (cannot tell). One more start if it
    does not appear within REOPEN_WAIT_SECONDS: explorer.exe returns at once
    whether or not the app it was asked for ever opens."""
    could_tell = False
    started_once = False
    # A classic install is started through its stub launcher, AnthropicClaude\\claude.exe,
    # which is itself a Desktop path with no --type=: seeing it proves nothing (review M-5).
    launcher = _norm(cmd[0]) if cmd else ""
    stub = launcher if launcher.endswith("\\anthropicclaude\\claude.exe") else None
    for _ in range(2):
        try:
            started = bool(ops["relaunch"](cmd))
        except Exception:
            started = False
        if not started:
            continue
        started_once = True
        deadline = ops["monotonic"]() + REOPEN_WAIT_SECONDS
        while True:
            rows = ops["list_processes"]()
            if rows is not None:
                could_tell = True
                if any(_norm(m.get("ExecutablePath")) != stub for m in desktop_mains(rows)):
                    return True
            if ops["monotonic"]() >= deadline:
                break
            ops["sleep"](2)
    if started_once and not could_tell:
        return None
    return False


@contextlib.contextmanager
def _vaulter_log(root: Path):
    """Route log records (the install's and the channel check's) into vaulter.log
    for the length of the block, as apply_update's --auto CLI does."""
    root_logger = logging.getLogger()
    old_level = root_logger.level
    handler = None
    try:
        logs = root / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logs / "vaulter.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    except OSError:
        handler = None
    try:
        yield
    finally:
        if handler is not None:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def main(argv: list, *, project_root: Path | None = None, ops: dict | None = None) -> int:
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    effects = dict(DEFAULT_OPS)
    effects.update(ops or {})
    dry = "--dry-run" in argv
    ignore_idle = "--ignore-idle" in argv

    def say(message: str) -> None:
        _log_line(root, ("dry run: " if dry else "") + message)

    try:
        return _run(root, effects, dry, ignore_idle, say)
    except Exception as e:
        say(f"stopped by an unexpected error ({type(e).__name__})")
        if not dry:
            _error_daily(root, f"unexpected {type(e).__name__}",
                         f"the automatic restart check stopped with an unexpected error "
                         f"({type(e).__name__})")
        return 1


def _run(root: Path, ops: dict, dry: bool, ignore_idle: bool, say) -> int:
    if au.is_development_copy(root):
        say("development copy, not restarting")
        return 0

    if not dry:                         # staging writes pending_update, so never in a dry run
        with _vaulter_log(root):
            try:
                ops["channel_check"]()
            except Exception:
                pass                    # unreachable channel: the next run asks again

    pending = None
    try:
        ready = root / "data" / "pending_update" / "ready.json"
        if ready.exists():
            pending = json.loads(ready.read_text(encoding="utf-8"))
    except Exception:
        pending = None
    running = au.running_version(root)
    # A stage that cannot be installed must never close Claude: decided here,
    # before anything is stopped, by the installer's own rules.
    staged_v = au.staged_update_applicable(pending, running, root / "data") if pending else ""
    if not staged_v:
        pending = None                  # nothing, superseded, or set aside after failing
    else:
        problem = au.stage_problem(root / "data", pending, staged_v)
        if problem:
            say(f"update {staged_v} is staged but its package is "
                f"{'missing' if problem == 'missing' else 'unusable'}, so Claude is not restarted "
                f"for it" + ("" if dry else "; it will be downloaded again"))
            if not dry:
                with _vaulter_log(root):
                    au.discard_stage(root / "data", pending)
                    if problem != "missing" and not au.set_aside_after_failure(
                            root / "data", pending, staged_v, "its package was unusable"):
                        _error(root, f"the staged package for update {staged_v} was unusable; it "
                                     f"was thrown away and will be downloaded again")
            pending = None
    try:
        version_mtime = (root / "VERSION").stat().st_mtime
    except OSError:
        version_mtime = 0.0

    procs = ops["list_processes"]()
    if procs is None:
        waiting = au.staged_update_applicable(pending, running)
        if waiting:
            say(f"update {waiting} is waiting, but the running programs could not be "
                f"listed, so nothing was restarted")
        return 0
    desk = desktop_root(procs)
    started = _iso_to_epoch(desk.get("CreationDate")) if desk else None
    # An unknown start time cannot prove a hand-applied update is newer than Desktop.
    version = restart_owed(pending, running, version_mtime,
                           started if started is not None else float("inf"),
                           _startup_installed(root))
    if not version:
        return 0
    if not desktop_processes(procs):
        say(f"update {version} is waiting; Claude Desktop is not running, so it will "
            f"install when Claude next opens")
        return 0
    if desk is None:
        say(f"update {version} is waiting, but Claude Desktop's main process could not be "
            f"told apart from its helpers ({len(desktop_mains(procs))} candidates), so nothing "
            f"was restarted")
        return 0

    state = _read_state(root)
    if restarted_since_give_up(state, version, started):
        state = _reset_give_up(state, version)
        if not dry:
            _write_state(root, state)
        say(f"update {version}: Claude Desktop was restarted since automatic restarts stopped, "
            f"so they start again")

    if not ignore_idle:
        if _just_woke(root, ops, dry):
            say(f"update {version} is waiting; the computer has just woken from sleep, so its "
                f"idle time does not count yet, trying again next run")
            return 0
        idle = float(ops["idle_seconds"]())
        if idle < IDLE_SECONDS_NEEDED:
            say(f"update {version} is waiting; the computer is in use (idle {int(idle)} of "
                f"{IDLE_SECONDS_NEEDED} seconds), trying again next run")
            return 0

    done = _restarts(state, version).get("restarts", 0)
    if not may_restart(state, version, RESTART_LIMIT):
        if _restarts(state, version).get("gave_up"):
            say(f"update {version}: automatic restarts already stopped after {done}, not restarting")
        elif dry:
            say(f"would stop restarting for update {version} after {done} restarts and report it")
        else:
            _error(root, f"Claude Desktop was restarted {done} times to load update {version} and "
                         f"it has still not taken effect; automatic restarts have stopped for this "
                         f"version. Quit and reopen Claude Desktop by hand.")
            _write_state(root, _gave_up(state, version, float(ops["time"]())))
            say(f"update {version}: stopped restarting after {done} restarts and told the team")
        return 0

    exe = str(desk.get("ExecutablePath") or "")
    apps = ops["start_apps"]() if "\\windowsapps\\" in _norm(exe) else []
    cmd = relaunch_command(exe, apps)
    if not cmd:
        say(f"update {version} is waiting, but there is no known way to reopen Claude "
            f"Desktop afterwards, so it was not closed")
        return 0

    attempt = int(done or 0) + 1
    if dry:
        say(f"would restart Claude Desktop for update {version} (restart {attempt} of "
            f"{RESTART_LIMIT}): close it, install, reopen it")
        return 0

    pid = _int(desk.get("ProcessId"))
    if pid <= 0 or not is_desktop_exe(exe):
        return 0
    # The pid could have been reused since the first listing (Desktop quit and
    # something else started). Stop it only if the same process -- same pid AND
    # same start time -- is still a Desktop executable.
    again = ops["list_processes"]() or []
    if not any(_int(p.get("ProcessId")) == pid and p.get("CreationDate") == desk.get("CreationDate")
               and is_desktop_exe(p.get("ExecutablePath")) for p in again if isinstance(p, dict)):
        say(f"update {version}: Claude Desktop changed while this check ran, so nothing was "
            f"stopped; trying again next run")
        return 0
    # Counted BEFORE anything is stopped: a run that dies part-way still counts,
    # so the loop guard holds whatever happens next.
    _write_state(root, record_restart(state, version))

    # Only THIS Desktop's tree is waited for. Waiting for every Desktop
    # executable meant an orphaned helper left by an older Desktop (its main long
    # gone) kept the install from ever happening.
    main_row = next(p for p in again if isinstance(p, dict) and _int(p.get("ProcessId")) == pid
                    and p.get("CreationDate") == desk.get("CreationDate"))
    main_key = _key(main_row)
    tree = tree_keys(again, main_row)
    tree_desktop = {_key(p) for p in again if isinstance(p, dict) and _key(p) in tree
                    and is_desktop_exe(p.get("ExecutablePath"))}

    outcome = "not installed"
    reopen_needed = True
    try:
        ops["taskkill"](pid)
        # Wall time, not a count of sleeps: each listing is a PowerShell run that
        # can take seconds, and the task's own time limit must never be reached
        # with Desktop closed.
        gone = False
        rows = None
        deadline = ops["monotonic"]() + EXIT_WAIT_SECONDS
        while True:
            rows = ops["list_processes"]()
            if rows is not None and not any(isinstance(p, dict) and _key(p) in tree_desktop
                                            for p in rows):
                gone = True
                break
            if ops["monotonic"]() >= deadline:
                break
            ops["sleep"](1)
        new_mains = {_key(p) for p in desktop_mains(rows) if _key(p) not in tree} if gone else set()
        reopened_meanwhile = False
        if new_mains:
            # Seen once is not enough: a short-lived claude.exe with no --type=
            # (a Squirrel hook, the stub launcher) would otherwise leave Claude
            # closed (review M-4). It must still be there a few seconds later.
            ops["sleep"](REOPENED_RECHECK_SECONDS)
            again_rows = ops["list_processes"]()
            reopened_meanwhile = again_rows is None or bool(
                new_mains & {_key(p) for p in desktop_mains(again_rows)})
        if not gone:
            outcome = "not installed, Claude Desktop did not close"
            # Reopen unless its main process is still there: helpers hanging on
            # alone leave no app to come back to, and when the list could not be
            # read at all, a reopen of a running Desktop only brings it forward.
            reopen_needed = rows is None or not any(
                isinstance(p, dict) and _key(p) == main_key for p in rows)
            _error(root, f"Claude Desktop did not close within {EXIT_WAIT_SECONDS} seconds for "
                         f"update {version}; nothing was installed")
        elif reopened_meanwhile:
            # The person came back and opened it while it was closed: installing
            # now would change files under its new server. The next idle run will.
            outcome = "not installed, Claude Desktop was opened again meanwhile"
            reopen_needed = False
        else:
            with _vaulter_log(root):
                recovered = ops["recover"](root)
                result = ops["auto_apply"](root, at_startup=False) or {}
            if result.get("applied"):
                outcome = "installed"
            elif result.get("restored"):
                outcome = "failed and was undone"
            elif result.get("version"):
                outcome = "not installed"
            else:
                outcome = "nothing to install"
            if recovered:
                outcome += ", an earlier unfinished install was undone first"
    finally:
        if not reopen_needed:
            reopened_text = "not needed, it is running"
        else:
            reopened = _reopen(ops, cmd)
            if reopened is False:
                _error(root, f"Claude Desktop was closed to load update {version} but could not be "
                             f"reopened; open it by hand")
            reopened_text = {True: "yes", False: "no", None: "started, not confirmed"}[reopened]
        say(f"restarted Claude Desktop for update {version} (restart {attempt} of "
            f"{RESTART_LIMIT}): {outcome}; reopened: {reopened_text}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
