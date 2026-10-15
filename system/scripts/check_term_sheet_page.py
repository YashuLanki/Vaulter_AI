"""
Does the mini term sheet dashboard page work in a browser?

Opens a generated page in headless Microsoft Edge and drives it: it draws with
no script error, the tabs and rows are there, the search finds a row, the
copy button shows its text in a box. Exists for the same reason
check_report_page.py does: this JavaScript is run by nothing else.

    python system/scripts/check_term_sheet_page.py            # the newest page in the team folder
    python system/scripts/check_term_sheet_page.py page.html  # a given page

Skips (exit 0) on a machine without Edge.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]

TEST = """
<script>
(async () => {
  const o = {errors: window.__errs || []};
  try {
    o.tabs = document.querySelectorAll("nav.tabs button").length;
    o.rows = document.querySelectorAll("table.rows tr.row").length;
    o.name = document.querySelector("h1").textContent;
    o.dashes = /\\s[\\u2014\\u2013]\\s/.test(document.body.innerText);
    const q = document.getElementById("q");
    q.value = "zoning"; q.dispatchEvent(new Event("input"));
    o.hits = document.querySelectorAll("#search .hit").length;
    document.querySelectorAll("nav.tabs button")[4].click();
    o.legalShown = !document.querySelector('section[data-tab="Legal"]').hidden;
    document.getElementById("copy").click();
    o.copyBox = (document.getElementById("copybox") || {value: ""}).value.length;
    o.counts = window.__mts;
  } catch (e) { o.exception = String(e); }
  document.title = "TESTOUT" + JSON.stringify(o);
})();
</script>"""


def main() -> int:
    edge = next((e for e in EDGE if e.exists()), None)
    if not edge:
        print("SKIP -- Microsoft Edge not found on this machine.")
        return 0
    if len(sys.argv) > 1:
        page_path = Path(sys.argv[1])
    else:
        import config
        pages = sorted(Path(config.MINI_TERM_SHEETS_DIR).rglob("*.html"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        pages = [p for p in pages if "_reference" not in p.parts]
        if not pages:
            print("SKIP -- no dashboard page in the team folder yet.")
            return 0
        page_path = pages[0]
    print("checking:", page_path)
    page = page_path.read_text(encoding="utf-8")
    guard = ("<script>window.__errs=[];window.addEventListener('error',"
             "e=>window.__errs.push(e.message+' line '+e.lineno));</script>")
    page = guard + page
    page = page.replace("</body>", TEST + "</body>") if "</body>" in page else page + TEST
    tmp = Path(tempfile.mkdtemp())
    test_page = tmp / "test.html"
    test_page.write_text(page, encoding="utf-8")
    r = subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
                        "--virtual-time-budget=8000", "--dump-dom", test_page.as_uri()],
                       capture_output=True, text=True, encoding="utf-8", timeout=180,
                       stdin=subprocess.DEVNULL)
    m = re.search(r"<title>TESTOUT(.*?)</title>", r.stdout, re.S)
    if not m:
        if not r.stdout.strip():
            # Edge hands a headless request to an instance already open and
            # prints nothing: measured 2026-10-12 with 41 Edge processes running.
            print("SKIP -- Edge returned no page at all. Microsoft Edge is probably open on this "
                  "machine; close every Edge window and run this again.")
            return 0
        print("FAIL -- the page produced no test result (it may not have loaded at all).")
        return 1
    o = json.loads(html.unescape(m.group(1)))
    checks = [
        ("the page draws with no script error", o.get("errors") == [] and "exception" not in o,
         o.get("exception") or o.get("errors")),
        ("seven tabs and every row are drawn", o.get("tabs") == 7 and o.get("rows", 0) > 50,
         f"{o.get('tabs')} tabs, {o.get('rows')} rows"),
        ("the property's name heads the page", bool(o.get("name", "").strip()), o.get("name")),
        ("no dash between words anywhere on the page", o.get("dashes") is False, o.get("dashes")),
        ("the search finds rows", o.get("hits", 0) > 0, o.get("hits")),
        ("a tab click shows that tab", o.get("legalShown") is True, o.get("legalShown")),
        ("the copy button shows its text in a box", o.get("copyBox", 0) > 20, o.get("copyBox")),
    ]
    failed = 0
    for name, ok, detail in checks:
        print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  -- {detail}"))
        failed += not ok
    print(f"{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
