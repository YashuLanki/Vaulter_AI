"""
team_status.py
--------------
Gather everything known about every teammate's install, in one pass.

Run by the daily 8am round (see scripts/daily_round.cmd), and safe to run by
hand any time -- it reads, and writes only its own summary.

WHY A DETERMINISTIC COLLECTOR AND NOT JUST AN AGENT: the facts here are
countable -- versions, dates, error counts, whether a check passed. Gathering
them costs nothing and cannot be got wrong. The judgement on top of them --
"Ava has not opened it in three days AND is two versions behind AND had an
error, so she is worth a nudge" -- is what the agent is for. Splitting it this
way means the numbers are never invented, and the morning report still contains
the raw facts even if the agent layer fails entirely.

Everything it reads lives in the shared folder, which syncs to whoever supports
this, so no machine has to be reachable and nobody has to be asked for anything.

    python system/scripts/team_status.py            # print it
    python system/scripts/team_status.py --json     # machine-readable
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# A machine quiet for longer than this has stopped being used, or has stopped
# working. Three days rather than one: a weekend, a day off or a busy Tuesday
# are all normal, and a check that cries wolf gets ignored -- the same reason
# the health check only warns about active-stage summaries.
QUIET_DAYS = 3

# Behind the published version for longer than this and something is wrong with
# the update path rather than with their timing.
STALE_VERSION_DAYS = 2

# How many days of dated briefings to keep in the team folder. `latest.md` is
# always the current one, so the dated copies exist only for looking back -- and
# a folder that gains a file a day, for ever, stops being something anyone opens.
# A month covers "what did it say last week" without becoming an archive nobody
# asked for.
KEEP_BRIEFING_DAYS = 30

# How large the local run log may get. It gains a few lines every morning and
# nothing ever removed them.
MAX_RUN_LOG_BYTES = 100_000

# A file list older than this is reported. Every answer about what documents
# exist is only as current as this list, and the worst wrong answer this system
# has ever given -- "no documents newer than <date>", when there were 57 -- came
# from reading a list that was itself eight days old while nothing looked wrong.
# So the age of the list is a fact the morning report states out loud, every day,
# rather than something anyone has to remember to check.
STALE_FILE_LIST_DAYS = 2


def _load_installs():
    from mcp_server import _read_installs
    return _read_installs()


def _published():
    from mcp_server import _published_version
    return _published_version()


def _days_since(raw):
    """Whole days since an ISO timestamp, or None if it cannot be read."""
    try:
        when = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
    return max(0, (now - when).days)


def _setup_records():
    """Who has run setup, from the records each install leaves behind."""
    from config import SHARED_DIR
    folder = Path(SHARED_DIR) / "system" / "setup_logs"
    out = {}
    try:
        for f in sorted(folder.glob("*.log")):
            person = f.name.split("--")[0]
            out.setdefault(person.lower(), []).append(f)
    except OSError:
        pass
    return out


def _error_reports():
    """Machines that have reported something broken, and how recently."""
    from config import SHARED_DIR
    folder = Path(SHARED_DIR) / "system" / "error_reports"
    out = []
    try:
        for f in sorted(folder.glob("*.log")):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entries = [ln for ln in text.splitlines() if ln.startswith("--- ")]
            out.append({
                "file": f.name,
                "person": f.name.split("--")[0],
                "entries": len(entries),
                "latest": entries[-1].strip("- ").strip() if entries else "",
                "days_old": _days_since(
                    datetime.fromtimestamp(f.stat().st_mtime).isoformat()),
            })
    except OSError:
        pass
    return out


def _crash_lines(r):
    """
    What a check said when it fell over instead of reporting.

    A crash writes to the error stream, which is captured separately from the
    normal output, so a caller reading only the normal output sees a failed exit
    code and nothing to explain it -- and then says "PROBLEM" with no cause.
    That is worse than saying nothing: it sends the reader looking for a fault
    that may not be where they think. Show the last few lines of whatever it
    actually said.
    """
    err = [ln.strip() for ln in (r.stderr or "").splitlines() if ln.strip()]
    out = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    lines = (err or out)[-6:]
    if not lines:
        return [f"stopped with exit code {r.returncode} and said nothing at all"]
    return [f"it stopped with exit code {r.returncode}; last thing it said:"] + lines


def _answers_check():
    """
    The free check on the knowledge answers are built from -- do cited documents
    exist, can every summary be dated, does every one declare what it did not
    read. Reads file NAMES only; opens no documents and calls nothing.
    """
    script = PROJECT_ROOT / "scripts" / "check_answers.py"
    if not script.exists():
        return {"ran": False, "reason": "not present"}
    try:
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"}
    tail = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()][-6:]
    # If it failed and printed nothing useful, it crashed rather than reported.
    # Show the crash. Saying "FAILED" with no reason attached is the one thing
    # a check like this must never do.
    if r.returncode != 0 and not tail:
        tail = _crash_lines(r)
    return {"ran": True, "passed": r.returncode == 0, "summary": tail}


def _connector_check():
    """Is the connector on THIS machine reachable and fast. Only this machine --
    nothing can reach into somebody else's."""
    script = PROJECT_ROOT / "scripts" / "check_mcp_health.py"
    if not script.exists():
        return {"ran": False, "reason": "not present"}
    try:
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"}
    problems = [ln.strip(" -") for ln in (r.stdout or "").splitlines()
                if ln.strip().startswith("- ")]
    if r.returncode != 0 and not problems:
        problems = _crash_lines(r)
    return {"ran": True, "passed": r.returncode == 0, "problems": problems}


def _file_list_age():
    """
    How old the list of the firm's documents is, and how many files it holds.

    Checked directly, never inferred from whether the nightly refresh reported
    success -- on 2026-08-20 that refresh had been pointing at a folder deleted
    weeks earlier after an install test, and Windows still recorded every run as
    successful because the program it was told to run did not exist to fail. The
    only trustworthy evidence about a file list is the file list.
    """
    from config import BASE_DIR
    out = []
    seen = set()
    for db in (Path(BASE_DIR) / "data" / "corpus_index.db",
               Path.home() / "Vaulter AI" / "system" / "data" / "corpus_index.db"):
        try:
            key = db.resolve()
        except OSError:
            key = db
        if key in seen:
            continue
        seen.add(key)
        if not db.exists():
            out.append({"where": db.parent.parent.parent.name, "present": False})
            continue
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            con.close()
        except Exception:
            files = None
        age = _days_since(datetime.fromtimestamp(db.stat().st_mtime).isoformat())
        out.append({
            "where": db.parent.parent.parent.name,
            "present": True,
            "files": files,
            "days_old": age,
            "stale": age is not None and age >= STALE_FILE_LIST_DAYS,
        })
    return out


def collect() -> dict:
    from mcp_server import _install_problems

    published = _published()
    installs = _load_installs()
    setups = _setup_records()

    people = []
    seen_people = set()
    for rec in installs:
        person = str(rec.get("user") or "unknown")
        seen_people.add(person.lower())
        quiet = _days_since(rec.get("last_seen"))
        version = rec.get("version") or "unknown"
        flags = _install_problems(rec)

        state = "fine"
        if quiet is not None and quiet >= QUIET_DAYS:
            state = "quiet"
        if published and version != published:
            built = _days_since(rec.get("version_built"))
            if built is not None and built >= STALE_VERSION_DAYS:
                state = "not updating"
        if flags:
            state = "needs attention"

        people.append({
            "person": person,
            "machine": rec.get("machine"),
            "install_folder": rec.get("install_folder"),
            "version": version,
            "up_to_date": bool(published) and version == published,
            "days_since_used": quiet,
            "problems": flags,
            "state": state,
        })

    # Installed but never seen: a setup record with no check-in at all. This is
    # the "completely dead" case -- the machine cannot report for itself, so its
    # absence is the only evidence there is.
    never_seen = [p for p in setups if p not in seen_people]

    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "published_version": published,
        "people": sorted(people, key=lambda p: p["person"].lower()),
        "installed_but_never_opened": never_seen,
        "error_reports": _error_reports(),
        "file_lists": _file_list_age(),
        "answers_check": _answers_check(),
        "connector_check": _connector_check(),
    }


def as_text(data: dict) -> str:
    lines = []
    lines.append(f"Vaulter AI - team status, {data['when'][:16].replace('T', ' ')}")
    lines.append(f"Newest published version: {data['published_version'] or 'unknown'}")
    lines.append("")

    if not data["people"]:
        lines.append("No machine has reported in. That is not the same as nobody")
        lines.append("having it installed -- a machine only appears here once it has")
        lines.append("opened a conversation.")
    for p in data["people"]:
        used = ("today" if p["days_since_used"] == 0
                else f"{p['days_since_used']} days ago" if p["days_since_used"] is not None
                else "unknown")
        # The folder is named too, because one person can have two installs on
        # one machine and the list is useless if they look identical.
        where = f" (in {p['install_folder']})" if p.get("install_folder") else ""
        lines.append(f"{p['person']} on {p['machine']}{where} - {p['state'].upper()}")
        # "reported" rather than "is", because that is all this can honestly say.
        # A machine writes its note when a conversation starts, so between an
        # update and the next conversation the note names the OLD version. That
        # actually happened on 2026-08-20: this list said a machine was two
        # versions back when it had been updated an hour earlier. For a list
        # whose whole job is spotting out-of-date machines, stating a stale
        # reading as current is the wrong way round -- so the reading now comes
        # with when it was taken.
        lines.append(f"    reported version {p['version']}"
                     + (" (up to date)" if p["up_to_date"] else " (behind)")
                     + f", as of {used}")
        for flag in p["problems"]:
            lines.append(f"    NEEDS ATTENTION: {flag}")

    if data["installed_but_never_opened"]:
        lines.append("")
        lines.append("Ran setup but has NEVER opened a conversation "
                     "(so their machine has never reported in):")
        for person in data["installed_but_never_opened"]:
            lines.append(f"    {person}")

    if data["error_reports"]:
        lines.append("")
        lines.append("Machines that reported something broken:")
        for e in data["error_reports"]:
            lines.append(f"    {e['person']}: {e['entries']} occasion(s), "
                         f"most recent {e['latest'] or 'unknown'}")

    lines.append("")
    for fl in data.get("file_lists", []):
        if not fl["present"]:
            lines.append(f"File list ({fl['where']}): not built on this machine")
            continue
        age = ("today" if fl["days_old"] == 0
               else f"{fl['days_old']} days old" if fl["days_old"] is not None
               else "age unknown")
        count = f"{fl['files']:,} files" if fl["files"] is not None else "could not be counted"
        flag = "  <-- TOO OLD, answers about what documents exist may be wrong" if fl["stale"] else ""
        lines.append(f"File list ({fl['where']}): {count}, refreshed {age}{flag}")

    a = data["answers_check"]
    lines.append("")
    if not a.get("ran"):
        lines.append(f"Answer quality check: could not run ({a.get('reason')})")
    else:
        lines.append(f"Answer quality check: {'passed' if a['passed'] else 'FAILED'}")
        for s in a.get("summary", []):
            lines.append(f"    {s}")

    c = data["connector_check"]
    if not c.get("ran"):
        lines.append(f"Connector on this machine: could not run ({c.get('reason')})")
    else:
        lines.append(f"Connector on this machine: {'healthy' if c['passed'] else 'PROBLEM'}")
        for p in c.get("problems", []):
            lines.append(f"    {p}")

    return "\n".join(lines)


def tidy_up() -> list:
    """
    Keep the things this routine writes from growing for ever.

    Two of them would have. The team folder gains one dated briefing a day, and
    the local run log gains a few lines a day with nothing ever taking any away.
    Neither is large quickly, and that is exactly why neither would have been
    noticed until the folder was unusable.

    Deliberately only touches files this routine itself produces, and never
    `latest.md`. Reports what it removed rather than doing it silently -- a
    cleanup nobody can see is indistinguishable from files going missing.
    """
    from config import SHARED_DIR
    said = []

    briefings = Path(SHARED_DIR) / "system" / "daily_status"
    try:
        dated = sorted(f for f in briefings.glob("*.md") if f.name != "latest.md")
        surplus = dated[:-KEEP_BRIEFING_DAYS] if len(dated) > KEEP_BRIEFING_DAYS else []
        removed = 0
        for f in surplus:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        if removed:
            said.append(f"removed {removed} briefing(s) older than "
                        f"{KEEP_BRIEFING_DAYS} days; kept {len(dated) - removed}")
    except OSError:
        pass

    runlog = Path(__file__).parent.parent / "data" / "logs" / "daily_round.log"
    try:
        if runlog.exists() and runlog.stat().st_size > MAX_RUN_LOG_BYTES:
            text = runlog.read_text(encoding="utf-8", errors="replace")
            # Cut at a line boundary, so the oldest surviving entry is a whole
            # line rather than half of one.
            kept = text[-MAX_RUN_LOG_BYTES:]
            cut = kept.find("\n")
            if cut != -1:
                kept = kept[cut + 1:]
            runlog.write_text(
                "(older entries removed to keep this file small)\n" + kept,
                encoding="utf-8")
            said.append("trimmed the run log")
    except OSError:
        pass

    return said


def main() -> int:
    for line in tidy_up():
        print(f"(housekeeping: {line})")
    data = collect()
    if "--json" in sys.argv:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(as_text(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
