"""
test_screening.py
------------------
Quick standalone smoke test for the merged 4-phase screening pipeline —
run this BEFORE wiring things up through Claude Desktop/MCP, so you can
iterate fast without restarting the MCP server every time.

Usage:
    1. Drop this file into your vaulter_ai project ROOT (next to main.py).
    2. Fill in ANTHROPIC_API_KEY / GOOGLE_MAPS_API_KEY below, or just make
       sure your confidentials/.env has them set (this script loads it the
       same way config.py does).
    3. Point COSTAR_FILE at a real CoStar export on disk.
    4. Run:  python test_screening.py

Cost heads-up: this makes real API calls — roughly 10 Claude calls for
Phase 3 (top 10 listings, ~$0.15-0.25 total) and up to 5 more + Google
Maps calls for Phase 4 (~$0-1.50 total, depending which Maps APIs are
enabled on your key). Leave GOOGLE_MAPS_API_KEY blank to skip Phase 4's
enrichment and just test Phases 1-3 for free-ish/cheap iteration.
"""

import sys
from pathlib import Path

# Make sure the project root is on the path so `analysis.screening...` resolves
sys.path.insert(0, str(Path(__file__).parent))

from pathlib import Path
from analysis.screening.pipeline import run_full_screening
import config  # loads confidentials/.env the same way mcp_server.py does

# ── Fill these in (or leave as-is to pull from confidentials/.env) ─────
ANTHROPIC_API_KEY = config.ANTHROPIC_API_KEY
GOOGLE_MAPS_API_KEY = config.GOOGLE_MAPS_API_KEY or None  # None = skip Phase 4 enrichment
COSTAR_FILE = Path("data/watched_folder/unknown/CostarExport.xlsx")  # <-- point this at a real file
TOP_N = 10

if __name__ == "__main__":
    if not ANTHROPIC_API_KEY:
        sys.exit("ANTHROPIC_API_KEY is not set — check confidentials/.env")
    if not COSTAR_FILE.exists():
        sys.exit(f"Can't find {COSTAR_FILE} — update COSTAR_FILE to point at a real CoStar export")

    print(f"Running full screening pipeline on {COSTAR_FILE} ...")
    result = run_full_screening(
        source_path=COSTAR_FILE,
        anthropic_api_key=ANTHROPIC_API_KEY,
        google_api_key=GOOGLE_MAPS_API_KEY,
        top_n=TOP_N,
    )

    print("\n" + "=" * 60)
    print(f"Market              : {result['market']}")
    print(f"Total screened      : {result['total_screened']}")
    print(f"Phase 1 survivors   : {result['phase1_survivors']}")
    print(f"Reached top {TOP_N}       : {len(result['top10_addresses'])}")
    print(f"Reached finalists   : {len(result['finalist_addresses'])}")
    print(f"Workbook written to : {result['workbook_path']}")
    print("=" * 60)
    print("\nTop candidates:")
    for c in result["top_candidates"]:
        print(f"  - {c['address']} | Composite: {c.get('composite_score')} | {c.get('recommendation_snippet')}")

    print("\nOpen the workbook to check all 4 tabs (Phase4 -> Phase1 order), then try")
    print("the dashboard by calling open_screening_dashboard from Claude, or run:")
    print("  python -c \"from analysis.screening.dashboard_server import start_dashboard_server; "
          "from pathlib import Path; import webbrowser; "
          "url = start_dashboard_server(Path('.')); webbrowser.open(url); input('Enter to exit...')\"")
