"""
The deal tracker's daily run, from Windows Task Scheduler on ONE machine
(owner, 2026-11-02: "schedule it on Ava's computer"). Registered by the
`schedule_deal_tracker` tool, never by hand. Same two layers as the morning round:

  1. Free, no model: Smartsheet, the summaries and any edits saved in the Excel
     copy are brought in, and the page and Excel copy rebuilt. Always runs.
  2. Reading new papers needs Claude. If Claude Code is installed here, it is
     asked to read and record whatever is waiting, through this program's own
     tools and nothing else. If it is not, the papers wait on the page and are
     read the next time someone opens the tracker in Claude Desktop.

The machine matters only because its OneDrive must be syncing the firm's library
(a machine whose sync broke saw nothing after 2026-10-17) and because the saved
email attachments live on it. Everything else is the same on any machine.

    python system/scripts/deal_tracker_daily.py            the run itself
"""
from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

log = logging.getLogger("deal_tracker_daily")
TASK_NAME = "Vaulter AI - Deal Tracker daily"

PROMPT = (
    "Bring the Vaulter deal tracker up to date. Call get_deal_tracker_documents and record each document "
    "it returns exactly as its instructions say (closing statements with save_deal_tracker_update, offer "
    "papers with save_deal_pipeline_update). Use only figures printed in the documents. A document that "
    "cannot be read is skipped, not guessed at. Repeat until nothing is waiting, at most 12 documents. "
    "Do not change anything else, and do not use any other tool. End with one line saying what was added."
)
TOOLS = ["get_deal_tracker_documents", "save_deal_tracker_update", "save_deal_pipeline_update"]


def layer_one() -> int:
    """Refresh and rebuild; return how many documents are waiting, or -1 if there is no tracker."""
    from deal_tracker import dashboard, tracker
    rec = tracker.load()
    if rec is None:
        log.info("No deal tracker in the team folder yet; nothing to do.")
        return -1
    # One machine runs this. If the schedule was moved elsewhere, this machine stands down,
    # so two computers never update the tracker at the same moment.
    here = platform.node().strip() or os.environ.get("COMPUTERNAME", "")
    owner = (rec.get("daily_run") or {}).get("machine")
    if owner != here:
        log.info(f"The daily run is {'on ' + owner if owner else 'switched off'}; this machine ({here}) does nothing.")
        return -1
    counts = tracker.refresh(rec)
    from deal_tracker import smartsheet_dashboards   # needs this machine's Smartsheet key; skips without one
    counts["dashboards"] = smartsheet_dashboards.refresh(rec)
    tracker.note_inbox(rec)
    tracker.save(rec)
    page = dashboard.write(rec)
    waiting = len(tracker.pending(rec))
    log.info(f"Refreshed ({counts}); page rebuilt at {page}; {waiting} document(s) waiting to be read.")
    return waiting


def layer_two() -> None:
    """Have Claude Code read the waiting papers through this program's own tools, if it is installed."""
    claude = shutil.which("claude")
    if not claude:
        log.info("Claude Code is not installed here; the waiting papers will be read when the tracker is "
                 "next opened in Claude Desktop.")
        return
    root = Path(__file__).resolve().parent.parent
    cfg = Path(config.DATA_DIR) / "deal_tracker_mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"vaulter_ai": {
        "command": sys.executable.replace("pythonw.exe", "python.exe"),
        "args": [str(root / "main.py"), "mcp"]}}}), encoding="utf-8")
    # A key with no credit outranks the signed-in account (the morning round's 2026-08-20 lesson).
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    out = Path(config.LOG_DIR) / "deal_tracker_daily_claude.txt"
    with open(out, "w", encoding="utf-8") as fh:   # a file, never pipes (the 2026-08-21 lesson)
        r = subprocess.run([claude, "-p", PROMPT, "--mcp-config", str(cfg), "--strict-mcp-config",
                            "--allowed-tools", *[f"mcp__vaulter_ai__{t}" for t in TOOLS]],
                           stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                           env=env, timeout=1800, cwd=str(root))
    tail = out.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:] or ["(no output)"]
    log.info(f"Claude Code finished ({'ok' if r.returncode == 0 else f'exit {r.returncode}'}): {tail[0][:300]}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  [%(levelname)s]  %(message)s",
                        handlers=[logging.FileHandler(Path(config.LOG_DIR) / "deal_tracker_daily.log",
                                                      encoding="utf-8")])
    try:
        waiting = layer_one()
    except Exception as e:
        log.error(f"The daily refresh failed: {type(e).__name__}: {e}")
        return 1
    if waiting > 0:
        try:
            layer_two()
        except Exception as e:
            log.error(f"Reading the waiting papers failed; they stay waiting: {type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
