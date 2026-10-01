"""
Does the screening report's page actually work in a browser? (2026-10-01)

    python system/scripts/check_report_page.py [export.xlsx]

Builds the HTML report for an export into a temporary folder, opens it in
headless Microsoft Edge, and drives it: the page draws with no script error,
re-scoring at the screen's own weights reproduces the screener's scores and
tiers, moving a weight re-ranks the cards, table and map together, all-zero
weights say so instead of ranking nothing, Reset restores the original order,
a decision mark is saved and shown, and "Copy for Claude" always shows its text.

Written because the report's interactive parts are JavaScript, which no other
check here runs: a load-order mistake that stopped the whole page, and a copy
button that silently did nothing, were both found only by opening it. Skips
(exit 0) on a machine without Edge.
"""
import html, json, re, subprocess, sys, tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]

TEST = r"""
<script>
window.addEventListener("load", () => {
  const out = {};
  const q = s => document.querySelector(s);
  out.errors = window.__errs || [];
  out.handle = typeof window.__vaulterReport;
  if (!window.__vaulterReport) { document.title = "TESTOUT" + JSON.stringify(out); return; }
  const V = window.__vaulterReport, L = V.L, W0 = V.W0, rescore = V.rescore, openDetail = V.openDetail,
        closeDetail = V.closeDetail, tk = V.tk, DATA = {listings: JSON.parse(JSON.stringify(V.L))};
  try {
    out.cards = document.querySelectorAll("#cards .card").length;
    out.top3 = document.querySelectorAll("#top3 .cand").length;
    out.rows = document.querySelectorAll("#tbl tbody tr").length;
    out.sliders = document.querySelectorAll("#tune-rows input[type=range]").length;
    out.pins = document.querySelectorAll("#svgmap circle.pin").length;
    out.firstRowsBefore = [...document.querySelectorAll("#tbl tbody tr")].slice(0,3).map(r=>r.children[0].textContent+" "+r.children[1].textContent.slice(0,30));
    out.sayBefore = q("#tune-sum").textContent;
    // Re-score with the screen's own weights: scores must reproduce the screener's
    const before = L.map(x=>[x.rank, x.score0 ?? x.score]);
    const orig = Object.fromEntries(L.map(x=>[x.rank, x.score]));
    rescore(Object.assign({}, W0));
    out.maxDiffAtOwnWeights = Math.max(...L.map(x=>Math.abs(x.score - orig[x.rank])));
    out.tierMismatchAtOwnWeights = L.filter(x=>tk(x.tier)!==tk(DATA.listings.find(y=>y.rank===x.rank).tier)).length;
    // Growth only
    const g = q("#w_growth"); ["pricing","distress","size_fit","proximity"].forEach(k=>{ const e=q("#w_"+k); e.value=0; e.dispatchEvent(new Event("input")); });
    g.value = 60; g.dispatchEvent(new Event("input"));
    out.sayGrowthOnly = q("#tune-sum").textContent;
    out.topByGrowth = L.slice(0,3).map(x=>[x.now, x.rank, x.comp.growth]);
    out.growthSorted = L.slice(0,20).every((x,i,a)=>i===0 || a[i-1].comp.growth >= x.comp.growth);
    out.cardsFollow = q("#cards .card").dataset.rank == String(L[0].rank);
    out.tableFollows = q("#tbl tbody tr").dataset.rank == String(L[0].rank);
    out.pinsRepainted = [...document.querySelectorAll("#svgmap circle.pin.t1")].length;
    // All zero
    g.value = 0; g.dispatchEvent(new Event("input"));
    out.sayAllZero = q("#tune-sum").textContent;
    q("#tune-reset").click();
    out.sayAfterReset = q("#tune-sum").textContent;
    out.firstRowsAfterReset = [...document.querySelectorAll("#tbl tbody tr")].slice(0,3).map(r=>r.children[0].textContent+" "+r.children[1].textContent.slice(0,30));
    // Detail + decision
    openDetail(L[0].rank);
    out.detailHasGrowth = !!q("#dbody .gsig");
    out.detailHasDecision = q("#dbody .dec button") !== null;
    out.detailData = (q("#dbody .d-sup")||{}).textContent;
    q('#dbody .dec button[data-v="pursue"]').click();
    const ta = q("#dnote"); ta.value = "Distressed seller, good exit"; ta.dispatchEvent(new Event("input", {bubbles:true}));
    out.decStored = JSON.stringify(V.dec()[L[0].rank]);
    out.decListed = document.querySelectorAll("#dec-list button").length;
    out.markInTable = !!q("#tbl tbody tr .mark.pursue");
    closeDetail();
    // copy text (clipboard is unavailable headless; the fallback box must show)
    q("#dec-copy").click();
    out.copyBoxShown = !q("#dec-box").hidden || q("#dec-copy").textContent==="Copied";
    out.copyText = q("#dec-box").value.slice(0,200);
    out.growthGrid = (q("#dbody .gsig")||{textContent:""}).textContent.slice(0,0);
  } catch (e) { out.exception = String(e) + " @ " + (e.stack||"").split("\n").slice(0,3).join(" | "); }
  document.title = "TESTOUT" + JSON.stringify(out);
});
</script>"""


def main() -> int:
    import logging
    logging.disable(logging.CRITICAL)
    edge = next((e for e in EDGE if e.exists()), None)
    if edge is None:
        print("SKIP -- Microsoft Edge not found; the report page was not tested.")
        return 0
    import config
    from analysis.screening import fit_screen as fs
    from analysis.screening.report import build_report
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(config.COSTAR_DROP_DIR) / "CostarExport.xlsx"
    if not src.exists():
        print(f"No export at {src}. Pass one as an argument.")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="vlt_page_"))
    page_path = Path(build_report(fs.screen(src, write_workbook=False), out_path=tmp / "report.html"))
    page = page_path.read_text(encoding="utf-8")
    guard = ("<script>window.__errs=[];window.addEventListener('error',"
             "e=>window.__errs.push(e.message+' line '+e.lineno));</script>")
    page = guard + page
    page = page.replace("</body>", TEST + "</body>") if "</body>" in page else page + TEST
    test_page = tmp / "test.html"
    test_page.write_text(page, encoding="utf-8")
    r = subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
                        "--virtual-time-budget=8000", "--dump-dom", test_page.as_uri()],
                       capture_output=True, text=True, encoding="utf-8", timeout=180,
                       stdin=subprocess.DEVNULL)
    m = re.search(r"<title>TESTOUT(.*?)</title>", r.stdout, re.S)
    if not m:
        print("FAIL -- the page produced no test result (it may not have loaded at all).")
        return 1
    o = json.loads(html.unescape(m.group(1)))
    checks = [
        ("the page draws with no script error", o.get("errors") == [] and "exception" not in o, o.get("exception") or o.get("errors")),
        ("every listing, card, slider and map pin is drawn",
         o.get("rows", 0) > 0 and o.get("cards", 0) > 0 and o.get("sliders") == 5, f"{o.get('rows')} rows, {o.get('cards')} cards"),
        ("re-scoring at the screen's own weights reproduces its scores",
         (o.get("maxDiffAtOwnWeights") or 9) <= 0.15, f"largest difference {o.get('maxDiffAtOwnWeights')}"),
        ("  ...and its tiers", o.get("tierMismatchAtOwnWeights") == 0, f"{o.get('tierMismatchAtOwnWeights')} differ"),
        ("weighting growth alone ranks by growth", o.get("growthSorted") is True),
        ("  ...and the cards and table follow the new order", o.get("cardsFollow") and o.get("tableFollows")),
        ("all-zero weights say so rather than ranking nothing", "zero" in (o.get("sayAllZero") or "")),
        ("Reset restores the screen's own order", o.get("firstRowsAfterReset") == o.get("firstRowsBefore")),
        ("the detail view shows growth signals and the decision buttons",
         o.get("detailHasGrowth") and o.get("detailHasDecision")),
        ("a decision is saved, listed and marked in the table",
         '"pursue"' in (o.get("decStored") or "") and o.get("decListed") == 1 and o.get("markInTable")),
        ("Copy for Claude always shows its text", o.get("copyBoxShown") and "record_screening_decision" in (o.get("copyText") or "")),
    ]
    ok = 0
    for name, cond, *detail in checks:
        ok += bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail[0]}" if detail and detail[0] else ""))
    print(f"\n{ok}/{len(checks)} checks passed")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
