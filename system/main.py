"""
main.py
-------
Vaulter AI Property Intelligence System
----------------------------------------
Single entry point for the entire system.

Usage:
  python main.py mcp                    — start the MCP server (what Claude Desktop runs)
  python main.py index-corpus           — (re)build the document-library index
  python main.py search <text>          — search the document library by filename/path
  python main.py screen <file> [moic]   — rank a CoStar export by portfolio fit (free, no API)
  python main.py properties             — list the portfolio from the Project Master
  python main.py stats                  — show what this instance has available

The ingest / query / reindex / scrape / web-sources / email / auth /
property-scrape / schedule commands were removed in the 2026-07 rebuild,
along with the pipelines behind them. See docs/REBUILD_PLAN.md.
"""

import os
import sys
import logging
import logging.handlers  # submodule: `import logging` alone does NOT expose this
from pathlib import Path

# ─── Console encoding ──────────────────────────────────────────────
# Windows consoles default to a legacy codepage (e.g. cp1252), not UTF-8 --
# any print()/log message containing a symbol like an arrow, box-drawing
# character, or checkmark then crashes with UnicodeEncodeError. This project
# uses such symbols throughout its CLI output (this file, the setup wizard,
# etc.), and per the project's "never crash for non-technical staff"
# convention, this must degrade (replacing the unprintable character)
# rather than take down the whole command. reconfigure() exists on both
# streams since Python 3.7; guarded anyway in case stdout/stderr have been
# replaced with something that doesn't support it (e.g. some CI runners).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─── Lock working directory to project root ───────────────────────
os.chdir(str(Path(__file__).parent))

from config import LOG_DIR

# ─── Logging ──────────────────────────────────────────────────────
# When running as MCP server, only log to file — NOT stdout.
# stdout is used for MCP stdio transport; any extra output breaks the connection.
_mcp_mode = len(sys.argv) > 1 and sys.argv[1] == "mcp"

# Rotating, not a plain FileHandler: this was appending forever with no cap,
# and measured 5.5 MB / 46,700 lines after roughly a week on one machine.
# Nothing ever trims it, nobody looks at it, and it would grow unbounded on
# every teammate's install. 2 MB × 3 keeps enough history to diagnose the
# kind of problem this log is actually used for (the 2026-07-30 connector
# hang was found in the last few hundred lines) while staying bounded at 8 MB.
_handlers = [logging.handlers.RotatingFileHandler(
    LOG_DIR / "vaulter.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
if not _mcp_mode:
    _handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("vaulter")


# ══════════════════════════════════════════════════════════════════
# Document library
# ══════════════════════════════════════════════════════════════════

def cmd_index_corpus():
    """
    Build the searchable index of the firm's document library.

    Reads file and folder NAMES only -- it never opens a document, so nothing
    is pulled down from OneDrive. Takes a couple of minutes on a library this
    size (~500k files) and only needs re-running when documents are added or
    moved.
    """
    from config import CORPUS_DIR, CORPUS_AVAILABLE
    from corpus import build_index

    if not CORPUS_AVAILABLE:
        print(f"The document library isn't available at: {CORPUS_DIR}")
        print("Check that OneDrive is syncing the firm's document library,")
        print("or set VAULTER_CORPUS_DIR in confidentials/.env.")
        return

    print(f"Indexing {CORPUS_DIR}")
    print("Reading filenames only — no document contents are downloaded.")
    result = build_index()
    print(f"\nDone — {result['file_count']:,} files indexed in {result['build_seconds']:.0f}s.")


def cmd_search(query: str):
    from corpus import search
    try:
        hits = search(query, limit=30)
    except Exception as e:
        print(e)
        return
    if not hits:
        print(f"No documents matched '{query}'.")
        print("Note: this matches file and folder NAMES, not document text.")
        return
    print(f"\n{len(hits)} match(es) for '{query}':\n")
    for hit in hits:
        print(f"  {hit['path']}")


def cmd_screen(source: str, moic: float = 3.0):
    """Rank a CoStar export by fit against the existing portfolio. No API calls."""
    from pathlib import Path as _P
    from config import DROP_DIR, COSTAR_DROP_DIR
    from analysis.screening.fit_screen import screen

    path = _P(source)
    if not path.exists():
        # Local first (unchanged for an existing machine), then the team's
        # shared CoStar Drop folder -- same order the MCP tool uses.
        for candidate in (DROP_DIR / source, COSTAR_DROP_DIR / source):
            if candidate.exists():
                path = candidate
                break
        else:
            print(f"No such file: {source}")
            print(f"Drop CoStar exports into either:")
            print(f"  {COSTAR_DROP_DIR}   (shared with the team)")
            print(f"  {DROP_DIR}   (this machine only)")
            return

    r = screen(path, moic=moic)
    df = r["dataframe"]
    print(f"\nScreened {r['total_screened']} listings from {r['source']} at {moic:g}x MOIC")
    print(f"Markets: {', '.join(r['markets'][:6]) or 'unspecified'}")
    print(f"Compared against {r['holdings_used']} geocoded holdings\n")
    for tier, n in sorted(r["tier_counts"].items()):
        print(f"  {tier:20} {n:4d}")
    print("\nTop 15 by fit:\n")
    for _, row in df.head(15).iterrows():
        print(f"  {int(row['Rank']):3d}. [{row['Fit_Score']:5.1f}] {str(row.get('Property Address'))[:40]}")
        print(f"        {row['Why']}")
    print(f"\nWorkbook: {r.get('workbook_path')}\n")


def cmd_properties():
    from portfolio import load_all_properties
    props, sold = load_all_properties()
    print(f"\nVaulter AI Portfolio — {len(props)} active properties\n")
    by_state = {}
    for p in props:
        by_state.setdefault(p.get("state", "Unknown"), []).append(p)
    for state in sorted(by_state):
        print(f"  {state} ({len(by_state[state])}):")
        for p in by_state[state]:
            print(f"    · {p['name']} | {p.get('category', '')} | {p.get('city', '')}")
    if sold:
        print(f"\n  Sold / Inactive ({len(sold)}):")
        for p in sold:
            print(f"    · {p.get('name', '?')} | {p.get('state', '')}")


def cmd_stats():
    from config import CORPUS_DIR, CORPUS_AVAILABLE, SHARED_DIR, SHARED_DIR_IS_FALLBACK
    from corpus import index_age

    W = 60
    print(f"\n{'=' * W}")
    print("  Vaulter AI — Status")
    print(f"{'=' * W}")

    print(f"  Document library : {CORPUS_DIR if CORPUS_AVAILABLE else 'NOT AVAILABLE'}")
    age = index_age()
    if age:
        count, built = age
        print(f"  Index            : {count:,} files, built {built:%Y-%m-%d %H:%M} UTC")
    else:
        print("  Index            : not built — run 'python main.py index-corpus'")

    if SHARED_DIR_IS_FALLBACK:
        print("  Shared folder    : NOT connected (using a local-only fallback)")
    else:
        print(f"  Shared folder    : {SHARED_DIR}")

    try:
        from portfolio import load_properties
        props, source = load_properties()
        print(f"  Portfolio        : {len(props)} active properties from {source}")
    except Exception as e:
        print(f"  Portfolio        : unavailable ({e})")

    print(f"{'=' * W}\n")


# ══════════════════════════════════════════════════════════════════
# MCP Server
# ══════════════════════════════════════════════════════════════════

def cmd_mcp():
    log.info("=" * 60)
    log.info("  Vaulter AI — MCP Server")
    log.info("  Transport  : stdio (this machine's own Claude Desktop launches this process directly)")
    log.info("  Access     : local only — whoever is logged into this computer with")
    log.info("               Claude Desktop configured to run it. Nothing is exposed")
    log.info("               over the network, so there is no separate key/password,")
    log.info("               and no port to configure either.")
    log.info("  Connect via Claude Desktop → Settings → Developer → Edit Config")
    log.info("  (add this server's command/args — see mcp_server.py header for the exact entry)")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 60)

    try:
        from mcp_server import run_mcp_server
        run_mcp_server()
    except ImportError as e:
        log.error(f"Missing dependency: {e}")
        log.error("Run: pip install mcp[cli]")


# ══════════════════════════════════════════════════════════════════
# Entry Point
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "mcp":
        cmd_mcp()

    elif args[0] == "index-corpus":
        cmd_index_corpus()

    elif args[0] == "search":
        if len(args) < 2:
            print("Usage: python main.py search <your search terms>")
        else:
            cmd_search(" ".join(args[1:]))

    elif args[0] == "screen":
        if len(args) < 2:
            print("Usage: python main.py screen <costar-file> [moic]")
        else:
            try:
                moic = float(args[2]) if len(args) > 2 else 3.0
            except ValueError:
                print("moic must be a number, e.g. 2.5")
                sys.exit(1)
            cmd_screen(args[1], moic)

    elif args[0] == "properties":
        cmd_properties()

    elif args[0] == "stats":
        cmd_stats()

    elif args[0] in ("--help", "-h", "help"):
        print(__doc__)

    else:
        print(f"Unknown command: '{args[0]}'")
        print("Run 'python main.py --help' to see all commands.")
