"""
check_my_connection.py
----------------------
"Is Vaulter AI actually working on my computer?" -- answered in plain English,
by someone who will never open a terminal, and reported back to whoever supports
this so they do not have to ask.

WHY THIS EXISTS. On 2026-08-21 two teammates had both installed successfully and
neither had ever appeared in the team's install list. From the shared folder it
was impossible to tell the difference between "she has not opened a conversation
yet" and "the connector will not start on her machine" -- and those need
completely different help. The only existing check, check_mcp_health.py, prints
to a terminal and reports nowhere, so the answer lived on her computer and
nowhere else.

WHAT IT DOES NOT PROVE, stated up front because this is exactly the kind of check
that gets over-read: it starts the program the same way Claude Desktop starts it
and confirms the program itself works. It cannot confirm that Claude Desktop has
been restarted since setup, because nothing on this side can see that. So a PASS
here plus "still not in the list" means the answer is a restart, and that is
useful precisely because it rules the program out.

Run by double-clicking quick_start/"Check my connection", or:
    python system/scripts/check_my_connection.py
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

LINE = "=" * 64


def _say(msg=""):
    """Print for a person, never crashing on a console that cannot show a tick."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


def _run_real_check():
    """Drive the existing deterministic check. Returns (ok, output)."""
    script = PROJECT_ROOT / "scripts" / "check_mcp_health.py"
    if not script.exists():
        return None, "the checker itself is missing from this folder"
    try:
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=600,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "it did not finish within 10 minutes"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0, out


def _plain_english(ok, out):
    """Translate the check's own output into something worth reading."""
    if ok is None:
        return ["Could not run the check at all.", out]
    if ok:
        tools = ""
        for line in out.splitlines():
            if "tools" in line and "list_tools" in line:
                tools = line.strip()
        return [
            "Vaulter AI is working on this computer.",
            "",
            "The program started, answered, and all its tools are present.",
            f"   ({tools})" if tools else "",
            "",
            "If your name still is not showing on the team list, the remaining",
            "step is Claude Desktop itself: quit it COMPLETELY (not just the",
            "window -- check the tray by the clock) and open it again, then send",
            "any message. Nothing here can see whether that has been done.",
        ]
    # Failed -- surface what it actually said, never a guessed cause.
    detail = [l.strip() for l in out.splitlines() if l.strip().startswith("- ")]
    lines = [
        "Vaulter AI did NOT start properly on this computer.",
        "",
        "This is worth sending to whoever set it up. What the check reported:",
        "",
    ]
    lines += (detail or [l for l in out.splitlines() if l.strip()][-8:])
    lines += [
        "",
        "Nothing has been changed or broken by running this check -- it only looked.",
    ]
    return lines


def _report_to_team(ok, lines, out):
    """
    Leave the answer where whoever supports this can read it.

    Same folder-and-filename convention as the install records, so one person's
    two computers never overwrite each other.
    """
    try:
        from config import SHARED_DIR, SHARED_DIR_IS_FALLBACK
        if SHARED_DIR_IS_FALLBACK:
            return "the team folder is not connected on this computer, so the result stays local"
        from mcp_server import _install_record_name, _who, _where, _get_code_version
        folder = Path(SHARED_DIR) / "system" / "connection_checks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / _install_record_name().replace(".json", ".txt")
        verdict = ("WORKING" if ok else "NOT WORKING" if ok is False else "COULD NOT CHECK")
        body = [
            LINE,
            f"Vaulter AI connection check -- {verdict}",
            f"  Person   : {_who()}",
            f"  Computer : {_where()}",
            f"  When     : {datetime.now():%d %b %Y %H:%M}",
            f"  Version  : {_get_code_version()}",
            LINE,
            "",
            "NOTE: a WORKING result means the program itself starts and answers.",
            "It does NOT mean Claude Desktop has been restarted since setup --",
            "nothing on this side can see that.",
            "",
        ] + [l for l in lines if l is not None] + ["", LINE, "Full output:", "", out]
        path.write_text("\n".join(body), encoding="utf-8")
        return f"result sent to the team folder as {path.name}"
    except Exception as e:
        return f"could not send the result to the team folder ({type(e).__name__})"


def main() -> int:
    _say(LINE)
    _say("  Checking whether Vaulter AI works on this computer")
    _say(LINE)
    _say()
    _say("  Starting it the same way Claude Desktop does. This takes")
    _say("  under a minute. Nothing is downloaded and nothing is changed.")
    _say()

    ok, out = _run_real_check()
    lines = _plain_english(ok, out)

    _say(LINE)
    for l in lines:
        if l is not None and l != "":
            _say("  " + l)
        elif l == "":
            _say()
    _say(LINE)
    _say()
    _say("  " + _report_to_team(ok, lines, out))
    _say()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
