"""
Does the deal tracker still do what it was built to do?

Builds a made-up tracker in a temporary folder (every name here is invented;
nothing real is read or written) and checks the rules the owner asked for:
the old spreadsheet and the pipeline workbook come in right, a name that could
mean two deals is asked about rather than guessed, accounting-named files are
never offered and never listed, two closings on one day to two buyers are two
sales, a signed statement outranks a summary's figure, and only the page and
the Excel copy land in the tracker folder. Then the Excel copy round trip
(an edit saved in Excel comes back, nothing else changes), the workbook opened
in real Excel with no formula errors, and the page driven in headless Edge.

    python system/scripts/check_deal_tracker.py

The Excel and Edge parts skip (and say so) on a machine without them.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl  # noqa: E402

import config  # noqa: E402
from deal_tracker import dashboard as DB  # noqa: E402
from deal_tracker import excel as X  # noqa: E402
from deal_tracker import pipeline as P  # noqa: E402
from deal_tracker import tracker as T  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok, detail="") -> None:
    RESULTS.append((name, bool(ok), "" if ok else str(detail)))


def section(title: str) -> None:
    RESULTS.append((f"── {title}", None, ""))


# ─── Made-up inputs ───────────────────────────────────────────────────────────

D = _dt.datetime


def old_tracker(path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Deal #", "Property Name", "State", "Type", "Size", "Status", "Acquisition Debt",
               "Date of Initial Contribution", "Last Parcel Date Sold", "Contributions",
               "Distributions/Return", "Project IRR", "Excluded from Additional Analysis"])
    ws.append([1, "Cedar Bluff 40", "AZ", "Land", "40 acres", "S", None, D(2010, 3, 1), D(2016, 5, 1),
               1_000_000, 2_500_000, 0.15, ""])
    ws.append([2, "Willow Flat", "CO", "Land", "12 acres (30 lots)", "E", None, D(2012, 1, 1), None,
               500_000, "LOST", None, ""])
    ws.append([None, "TOTAL SOLD PROPERTIES", None, None, None, None, None, None, None, 1_500_000])
    ws.append([3, "Oak Ridge 40", "AZ", "Land", "40 acres", "", None, D(2018, 6, 1), "var", 2_000_000, 300_000])
    ws.append([4, "Oak Ridge 80", "AZ", "Land", "80 acres", "", None, D(2019, 2, 1), None, 3_000_000, None])
    ws.append([5, "Pine Flat 12 Investors, LLC", "TX", "Land", "12 acres", "", None, D(2021, 4, 15), None, 800_000])
    ws.append([None, "Total Lifetime Deals", None, None, None, None, None, None, None, 7_300_000])
    ws.append([6, "Should Not Import", "AZ"])
    wb.save(path)


def pipeline_book(path: Path, today: _dt.date) -> None:
    wb = openpyxl.Workbook()
    dash = wb.active
    dash.title = "Dashboard"
    for r in (["PIPELINE"], [], ["OPEN ITEMS"], ["Oak Ridge 40", "send the survey"], ["Call title"]):
        dash.append(r)
    dd = wb.create_sheet("Deal Detail")
    dd.append(["Deal Detail"])
    head = list(P.FIELDS)
    dd.append(head)

    def row(**v):
        dd.append([v.get(P.FIELDS[h]) for h in head])
    row(type="Disposition", property="Oak Ridge 40", parcel="Pcl 6", state="AZ", buyer="Granite Homes",
        stage="Under Contract", close_date=D.combine(today + _dt.timedelta(days=20), _dt.time()), price=1_200_000)
    row(type="Acquisition", property="Maple Draw", state="TX", seller="Smith Family", stage="LOI Sent",
        loi_date=D(2026, 9, 20))
    row(type="Disposition", property="Oak Ridge 80", state="AZ", buyer="Birch Builders", stage="Terminated")
    terms = wb.create_sheet("Offer Terms")
    terms.append(["Deal", "Oak Ridge – Pcl 6"])
    terms.append(["Price", "$1.2M"])
    terms.append(["Closing", "30 days after due diligence"])
    wb.save(path)


FILES = [  # the drive's file list, names only
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/Closing - Granite/SS/261010 Est SS.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/Closing - Granite/SS/261015 Settlement Statement Final.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/Closing - Granite/SS/261016 Settlement Statement - Accounting copy.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Closing Docs/261015 Settlement Statement Final.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Loan/261012 Settlement Statement.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/Closing - Granite/SS/201010 Settlement Statement.pdf",
    "!PROPERTIES/TEXAS PROPERTIES/Maple Draw/Acquisition/Closing/261020 Closing Statement Buyer.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/!OFFERS/2026 Granite Homes/261002 PSA draft.docx",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/!OFFERS/2026 Granite Homes/261008 PSA v3 redline.docx",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/!OFFERS/2026 Granite Homes/261009 PSA FE.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/!OFFERS/2026 Granite Homes/261011 PSA invoice.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 80/Disposition/!OFFERS/2026 Birch Builders/PSA/261003 LOI Birch.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 80/Disposition/!OFFERS/2026 Birch Builders/260901 LOI first.pdf",
]


def fake_index():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE files (path TEXT, name TEXT)")
    con.executemany("INSERT INTO files VALUES (?, ?)", [(p, p.rsplit("/", 1)[-1]) for p in FILES])
    return con


# ─── The checks ───────────────────────────────────────────────────────────────

def run(tmp: Path) -> None:
    from corpus import index
    import portfolio
    import summaries

    today = _dt.date.today()
    T.RECORD_FILE = tmp / "system" / "record.json"
    T.DO_NOT_OPEN_FILE = T.RECORD_FILE.with_name("do not open - words.txt")
    index._connect = fake_index
    inbox = tmp / "inbox"
    inbox.mkdir()
    (inbox / "Closing Statement Maple.pdf").write_bytes(b"%PDF-1.4")
    (inbox / "K-1 2025.pdf").write_bytes(b"%PDF-1.4")
    (inbox / "notes.xyz").write_text("x")
    config.DEAL_INBOX_DIR = inbox

    section("Starting from the old spreadsheet")
    old, book = tmp / "Old tracker.xlsx", tmp / "Pipeline Tracker_20261001.xlsx"
    old_tracker(old)
    pipeline_book(book, today)
    rec = T.import_tracker(old)
    names = [d["name"] for d in rec["deals"]]
    by = {d["name"]: d for d in rec["deals"]}
    check("every deal row comes in, totals and anything after them do not",
          names == ["Cedar Bluff 40", "Willow Flat", "Oak Ridge 40", "Oak Ridge 80", "Pine Flat 12 Investors, LLC"], names)
    check("rows above TOTAL SOLD are sold, rows below are current",
          [d["section"] for d in rec["deals"]] == ["sold", "sold", "current", "current", "current"])
    check("LOST is read as lost, not as a number", by["Willow Flat"]["lost"] and by["Willow Flat"]["distributions"] is None)
    check("'var' means sold in parts", by["Oak Ridge 40"]["sold_in_parts"])
    check("size gives acres and lots", (by["Willow Flat"]["acres"], by["Willow Flat"]["lots"]) == (12.0, 30))
    check("watching starts the day after the newest date", rec["watch_from"] == "2021-04-16", rec["watch_from"])

    T.add_pipeline(rec, book)
    pl = {d["property"]: d for d in rec["pipeline"]}
    check("three pipeline deals come in", len(rec["pipeline"]) == 3, len(rec["pipeline"]))
    check("papers are watched from the workbook's own date", rec["pipeline_watch_from"] == "2026-10-01",
          rec["pipeline_watch_from"])
    check("a short Offer Terms name finds its deal", pl["Oak Ridge 40"]["terms"].get("Price") == "$1.2M",
          pl["Oak Ridge 40"]["terms"])
    check("open items come from below OPEN ITEMS", rec["open_items"] == ["Oak Ridge 40: send the survey", "Call title"],
          rec["open_items"])

    section("Matching names, never guessing")
    check("Oak Ridge 40 never matches Oak Ridge 80", [d["name"] for d in T.matches(rec, "Oak Ridge 40")] == ["Oak Ridge 40"])
    check("company words are ignored", [d["name"] for d in T.matches(rec, "Pine Flat 12")] == ["Pine Flat 12 Investors, LLC"])
    check("a name that fits two deals returns both", len(T.matches(rec, "Oak Ridge")) == 2)

    portfolio.load_all_properties = lambda: (
        [{"name": "Oak Ridge 40", "category": "Disposition", "state": "Arizona"},
         {"name": "Cedar Bluff 40", "category": "Rezone", "state": "Arizona"},
         {"name": "Oak Ridge", "category": "Rezone", "state": "Arizona"}],
        [{"name": "Willow Flat", "state": "Colorado"}])
    summaries.load_cards = lambda folder: [
        {"property": "Pine Flat 12", "entry_price_usd": 750_000, "entry_year": 2021, "acres": 12.0,
         "outcome_status": "held", "filename": "pine-flat-12.md"}]
    T.refresh(rec)
    by = {d["name"]: d for d in rec["deals"] if d["section"] != "new"}
    new = [d["name"] for d in rec["deals"] if d["section"] == "new"]
    check("a live Smartsheet name sets the stage", by["Oak Ridge 40"]["stage"] == "Disposition")
    check("a live name never lands on an old sold deal", "Cedar Bluff 40" in new and not by["Cedar Bluff 40"]["stage"], new)
    check("an ambiguous name is asked about, not linked",
          any('"Oak Ridge"' in a for a in rec["attention"]) and "Oak Ridge" not in new, rec["attention"])
    check("a struck-through name marks the old deal sold", by["Willow Flat"]["smartsheet_sold"])
    check("a summary card fills an empty purchase price", by["Pine Flat 12 Investors, LLC"]["purchase_price"] == 750_000)

    section("Accounting files are never offered or listed")
    words = T.do_not_open_words()
    check("the word list is seeded beside the record", T.DO_NOT_OPEN_FILE.is_file() and "accounting" in words)
    check("accounting-type names are blocked",
          all(T.blocked(n, words) for n in ("K-1 2025.pdf", "Bank_Statement.pdf", "261011 PSA invoice.pdf")))
    check("a word inside another word does not block", not T.blocked("Ledgerwood Ranch Settlement Statement.pdf", words))
    items = T.pending(rec)
    listed = json.dumps(items)
    check("no blocked file appears anywhere in what is offered",
          not any(w in listed.lower() for w in ("accounting copy", "k-1", "invoice")), listed[:300])

    section("Closing statements found by name")
    closings = [i for i in items if i["key"].startswith("drive:")]
    docs = [i["document"].rsplit("/", 1)[-1] for i in closings]
    check("one sale and one purchase are offered", sorted(i["side"] for i in closings) == ["purchase", "sale"], docs)
    check("the final statement is picked over the estimate", "261015 Settlement Statement Final.pdf" in docs, docs)
    check("the same statement in two folders is offered once", docs.count("261015 Settlement Statement Final.pdf") == 1, docs)
    check("loan papers and statements filed before the start date are left out",
          not any("261012" in json.dumps(i) or "201010" in json.dumps(i) for i in closings))
    sale = next(i for i in closings if i["side"] == "sale")
    check("the property folder is named", sale["property_folder"] == "Oak Ridge 40", sale["property_folder"])

    section("Offer papers: one folder, newest paper")
    papers = [i for i in items if i["key"].startswith("paper:")]
    pd = {i["offer_folder"]: i["document"].rsplit("/", 1)[-1] for i in papers}
    check("the newest signed paper is picked in each offer folder",
          pd == {"2026 Granite Homes": "261009 PSA FE.pdf", "2026 Birch Builders": "261003 LOI Birch.pdf"}, pd)
    check("a PSA subfolder counts as its offer, not a buyer", "PSA" not in pd)

    section("Saved email attachments")
    inbox_items = [i for i in items if i["key"].startswith("inbox:")]
    check("only the readable, non-accounting attachment is offered",
          [i["document"] for i in inbox_items] == ["Closing Statement Maple.pdf"], [i["document"] for i in inbox_items])

    section("Marking read")
    check("a closing can be marked read", T.mark_read(rec, sale["key"], "sale recorded"))
    granite = next(i for i in papers if i["offer_folder"] == "2026 Granite Homes")
    check("an offer paper can be marked read", T.mark_read(rec, granite["key"], "pipeline updated"))
    check("an inbox file can be marked read", T.mark_read(rec, inbox_items[0]["key"], "purchase recorded"))
    left = {i["key"] for i in T.pending(rec)}
    check("what was read is not offered again",
          not ({sale["key"], granite["key"], inbox_items[0]["key"]} & left) and len(left) == 2, left)
    check("an unknown key is refused", not T.mark_read(rec, "drive:no such folder", "x"))

    section("Recording what a document says")
    pine, o80, o40 = by["Pine Flat 12 Investors, LLC"], by["Oak Ridge 80"], by["Oak Ridge 40"]
    T.record_event(rec, pine, "purchase", "2021-04-15", 752_000, None, "Seller Co", False, None, None, "Statement A")
    check("a signed statement replaces a summary's figure", pine["purchase_price"] == 752_000 and
          pine["sources"]["purchase_price"] == "Statement A")
    T.record_event(rec, pine, "purchase", "", 752_000.4, None, "", False, None, None, "Statement B")
    check("under a dollar is rounding, not a disagreement",
          not any("Pine Flat" in a and "gives" in a for a in rec["attention"]))
    T.record_event(rec, o80, "purchase", "2019-02-01", 2_400_000, None, "", False, None, None, "Statement C")
    said = T.record_event(rec, o80, "purchase", "", 2_600_000, None, "", False, None, None, "Statement D")
    check("a real disagreement keeps the old figure and asks a person",
          o80["purchase_price"] == 2_400_000 and any("Oak Ridge 80" in a and "2600000" in a for a in rec["attention"]), said)
    T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes LLC", False, None, None, "SS 1")
    again = T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes", False, None, None, "SS 1 copy")
    T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Birch Builders", False, None, None, "SS 2")
    check("the same sale read twice is recorded once", again == ["sale already recorded"], again)
    check("two closings on one day at one price to two buyers are two sales", len(o40["sales"]) == 2, o40["sales"])
    check("a part sale reads as partially sold", T.status(o40) == "Partially sold", T.status(o40))
    closed = P.close_from_statement(rec, o40, "sale", "2026-10-15", 600_000, "Granite Homes")
    check("the statement closes the matching pipeline deal",
          closed and pl["Oak Ridge 40"]["stage"] == "Closed" and pl["Oak Ridge 40"]["history_id"] == o40["id"], closed)
    check("a closed pipeline deal keeps its own price", pl["Oak Ridge 40"]["price"] == 1_200_000, pl["Oak Ridge 40"]["price"])

    section("Status")
    check("sold section reads fully exited", T.status(by["Cedar Bluff 40"]) == "Fully exited")
    check("lost reads lost", T.status(by["Willow Flat"]) == "Lost")
    check("held with nothing sold reads owned", T.status(pine) == "Owned")
    T.record_event(rec, o80, "sale", "2026-10-20", 900_000, 80, "Aspen Co", True, None, None, "SS 3")
    check("a final sale reads fully exited", T.status(o80) == "Fully exited")

    section("The page and the Excel copy")
    folder = tmp / "Deal Tracker"
    T.save(rec)
    DB.write(rec, folder)
    check("only the page and the Excel copy are in the tracker folder",
          sorted(p.name for p in folder.iterdir()) == ["Deal Tracker.html", "Deal Tracker.xlsx"],
          [p.name for p in folder.iterdir()])
    page = (folder / "Deal Tracker.html").read_text(encoding="utf-8")
    check("no blocked file name reaches the page", not re.search(r"accounting copy|k-1 2025|invoice", page, re.I))
    rows = {r["id"]: r for r in DB.build_payload(rec, today)["deals"]}   # by id: two deals share a name
    cb = rows[by["Cedar Bluff 40"]["id"]]
    check("profit is returned less put in", cb["profit"] == 1_500_000 and cb["multiple"] == 2.5, (cb["profit"], cb["multiple"]))
    check("hold runs from first money in to last sale", cb["hold"] == 6.2, cb["hold"])
    check("a lost deal shows no profit", rows[by["Willow Flat"]["id"]]["profit"] is None)
    yr = DB.deal_row({**by["Cedar Bluff 40"], "acquired": "2010"}, today)
    check("a year-only date is marked approximate", yr["acquired_approx"] and yr["acquired"] == "2010-07-01", yr["acquired"])

    section("Excel round trip")
    xl = folder / "Deal Tracker.xlsx"
    wb = openpyxl.load_workbook(xl)
    ws = wb["Deal Detail"]
    head_row = next(r for r in range(1, 40) if ws.cell(r, 1).value == "Type")
    idc = [c.column for c in ws[head_row] if c.value == X.ID_HEAD][0]
    maple = next(d for d in rec["pipeline"] if d["property"] == "Maple Draw")
    mrow = next(r for r in range(head_row + 1, ws.max_row + 1) if ws.cell(r, idc).value == maple["id"])
    ws.cell(mrow, X.COL["Next Step"], "Send a counter")
    ws.cell(mrow, X.COL["Days Out"], 999)        # a worked-out column: must be ignored
    band = next(r for r in range(1, ws.max_row + 1) if str(ws.cell(r, 1).value or "").startswith("ADD NEW DEALS"))
    ws.cell(band + 1, X.COL["Type"], "Acquisition")
    ws.cell(band + 1, X.COL["Property"], "Aspen Mesa")
    hs = wb["History"]
    hh = [c.value for c in hs[1]]
    hrow = next(r for r in range(2, hs.max_row + 1) if hs.cell(r, hh.index(X.ID_HEAD) + 1).value == o80["id"])
    hs.cell(hrow, hh.index("Equity put in") + 1, 3_100_000)
    wb["Open Items"].delete_rows(2)
    wb.save(xl)
    st = xl.stat()
    os.utime(xl, (st.st_atime, st.st_mtime + 5))   # a save within the same second would look unchanged
    said = X.pull_edits(rec, xl)
    check("an edited cell comes back", maple.get("next_step") == "Send a counter", said)
    check("a new row with no ID becomes a new deal", any(d["property"] == "Aspen Mesa" for d in rec["pipeline"]), said)
    check("a History edit comes back", o80["contributions"] == 3_100_000, said)
    check("a deleted open item stays deleted", rec["open_items"] == ["Call title"], rec["open_items"])
    check("nothing else is changed, worked-out columns included",
          sorted(said) == sorted(["Maple Draw: next step", "Aspen Mesa: added", "Oak Ridge 80: contributions",
                                  "open items"]), said)
    DB.write(rec, folder)
    check("an unchanged copy brings nothing back", X.pull_edits(rec, xl) == [])

    excel_check(xl)
    edge_check(folder / "Deal Tracker.html")


# ─── Real Excel and a real browser ────────────────────────────────────────────

PS_EXCEL = r"""
$ErrorActionPreference = 'Stop'
try { $x = New-Object -ComObject Excel.Application } catch { 'NOEXCEL'; exit }
$x.DisplayAlerts = $false
try {
  $wb = $x.Workbooks.Open('{path}', 0, $true)
  $x.CalculateFull()
  $n = 0
  foreach ($s in $wb.Worksheets) { foreach ($c in $s.UsedRange.Cells) { if ($c.Text -match '^#(VALUE|REF|NAME|DIV|N/A|NUM|NULL)') { $n++ } } }
  "ERRORS $n"
  $wb.Close($false)
} catch { "OPENFAIL " + $_.Exception.Message } finally { $x.Quit() }
"""


def excel_check(xl: Path) -> None:
    section("Opened in real Excel")
    out = xl.with_name("excel_check.txt")
    try:
        with open(out, "w", encoding="utf-8") as fh:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            PS_EXCEL.replace("{path}", str(xl).replace("'", "''"))],
                           stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, timeout=240)
        text = out.read_text(encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        text = f"OPENFAIL {e}"
    finally:
        out.unlink(missing_ok=True)
    if "NOEXCEL" in text:
        RESULTS.append(("Excel is not installed here; skipped", None, ""))
        return
    m = re.search(r"ERRORS (\d+)", text)
    check("Excel opens the workbook (one bad bracket makes it refuse the whole file)", m, text.strip()[:300])
    check("no formula shows an error", m and m.group(1) == "0", text.strip()[:300])


EDGE = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]

TEST = """
<script>
(() => {
  const o = {errors: window.__errs || []};
  try {
    const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
    o.tabs = $$("[role=tab]").length;
    o.prows = $$("tr[data-pid]").length;
    o.kpis = $$("#pk .kpi").length;
    o.open = $$("#open .item").length;
    $$("[role=tab]")[1].click();
    o.histShown = !$('section[data-view="history"]').hidden;
    o.hrows = $$("tr[data-id]").length;
    $('#period button[data-y="1"]').click();
    o.hrows1 = $$("tr[data-id]").length;
    $('#period button[data-y="0"]').click();
    const q = $("#q"); q.value = "oak ridge"; q.dispatchEvent(new Event("input"));
    o.hits = $$("#results button").length;
    $("#results button").click();
    o.drawer = $("#drawer").classList.contains("on");
    o.dashes = /\\s[\\u2014\\u2013-]\\s/.test(document.body.innerText);
    o.attention = !!$("#attention details");
  } catch (e) { o.exception = String(e); }
  document.title = "TESTOUT" + JSON.stringify(o);
})();
</script>"""


def edge_check(page_path: Path) -> None:
    section("The page in a browser")
    edge = next((e for e in EDGE if e.exists()), None)
    if not edge:
        RESULTS.append(("Microsoft Edge not found; skipped", None, ""))
        return
    page = page_path.read_text(encoding="utf-8")
    guard = ("<script>window.__errs=[];window.addEventListener('error',"
             "e=>window.__errs.push(e.message+' line '+e.lineno));</script>")
    test_page = page_path.with_name("test.html")
    test_page.write_text(guard + page.replace("</body>", TEST + "</body>"), encoding="utf-8")
    r = subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
                        "--virtual-time-budget=8000", "--dump-dom", test_page.as_uri()],
                       capture_output=True, text=True, encoding="utf-8", timeout=180, stdin=subprocess.DEVNULL)
    test_page.unlink(missing_ok=True)
    m = re.search(r"<title>TESTOUT(.*?)</title>", r.stdout, re.S)
    if not m:
        RESULTS.append(("Edge returned no page (is Edge open? close it and run again); skipped"
                        if not r.stdout.strip() else "FAIL: the page produced no test result",
                        None if not r.stdout.strip() else False, ""))
        return
    o = json.loads(html.unescape(m.group(1)))
    check("the page draws with no script error", o.get("errors") == [] and "exception" not in o,
          o.get("exception") or o.get("errors"))
    check("two tabs; pipeline rows, figures and open items drawn",
          o.get("tabs") == 2 and o.get("prows", 0) >= 3 and o.get("kpis") == 5 and o.get("open") == 1, o)
    check("the History tab opens and lists every deal", o.get("histShown") and o.get("hrows") == 6, o)
    check("the last-12-months view narrows the list", 0 < o.get("hrows1", 0) < o.get("hrows", 0), o)
    check("the search finds both tabs' deals and opens one", o.get("hits", 0) >= 3 and o.get("drawer"), o)
    check("no dash between words anywhere on the page", o.get("dashes") is False)
    check("the things needing a person are shown", o.get("attention"))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="deal_tracker_check_"))
    try:
        run(tmp)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"the checks ran to the end ({type(e).__name__}: {e})", False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = 0
    for name, ok, detail in RESULTS:
        if ok is None:
            print(("\n" if name.startswith("──") else "  SKIP  ") + name)
            continue
        print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  -- {detail}"))
        failed += not ok
    n = sum(1 for r in RESULTS if r[1] is not None)
    print(f"\n{n - failed}/{n} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
