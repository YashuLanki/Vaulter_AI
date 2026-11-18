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
import time
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
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Disposition/Closing - Granite/APV/261016 Affidavit of Property Value RECORDED.pdf",
    "!PROPERTIES/ARIZONA PROPERTIES/Oak Ridge 40/Loan/261012 Deed of Trust.pdf",
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
    from deal_tracker import inbox as IB
    T.CALENDAR_WORDS_FILE = T.RECORD_FILE.with_name("calendar words.txt")
    T.CALENDAR_WORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    T.CALENDAR_WORDS_FILE.write_text(
        "# made-up word list\n# --- words (edit freely) ---\ndd\nexpiration\nexpires\nclosing\nconfirm\n"
        "# --- deal names (kept current by the tracker's daily run; edits here are replaced) ---\noak ridge 40\n"
        "# --- other names (kept current by the tracker's daily run; edits here are replaced) ---\nsam\n",
        encoding="utf-8")
    inbox = tmp / "inbox"
    IB.make_workbooks(inbox)
    att = inbox / IB.ATTACH_DIR
    for code in ("a1", "a2", "a3"):
        (att / f"{code}.pdf").write_bytes(b"%PDF-1.4")
    (att / "notes.xyz").write_text("x")
    add_rows(inbox / IB.ATTACH_LOG, "Attachments", [
        ["Nov 17, 2026 1:00 PM", "A. Broker", "Maple", '=HYPERLINK("https://x/a1.pdf","Closing Statement Maple.pdf")', "", "New", "a1"],
        ["Nov 17, 2026 1:00 PM", "A. Broker", "Taxes", "K-1 2025.pdf", "", "New", "a2"],
        ["Nov 17, 2026 1:00 PM", "A. Broker", "Scan", "Scan_0012.pdf", "", "New", "a3"]])
    add_rows(inbox / IB.DATES_BOOK, "Calendar", [
        ["Nov 17, 2026 2:00 PM", "A. Friend", "Lunch", "Nov 20, 2026 12:00 PM", "Lunch.", "", "", "New", "c1"],
        ["Nov 17, 2026 2:00 PM", "T. Title", "Oak Ridge 40 DD expiration", "Dec 15, 2026 10:00 AM",
         "Due diligence on Oak Ridge 40 expires Dec 15.", "", "", "New", "c2"],
        ["Nov 17, 2026 2:00 PM", "A. Friend", "Call with Sam", "Nov 21, 2026 9:00 AM", "Catch up.", "", "", "New", "c3"]])
    add_rows(inbox / IB.DATES_BOOK, "Email", [
        ["Nov 17, 2026 3:00 PM", "A. Broker", "Oak Ridge 40 closing moved", "Closing moved to Jan 9.", "", "", "New", "e1"],
        ["Nov 17, 2026 3:00 PM", "A. Friend", "Team lunch confirm", "Please confirm.", "", "", "New", "e2"],
        ["Nov 17, 2026 3:00 PM", "A. Friend", "Sam says hi", "Lunch?", "", "", "New", "e3"],
        ["Nov 17, 2026 3:00 PM", "A. Broker", "Oak Ridge 40 papers", "Schedule K-1 attached; closing soon.", "", "", "New", "e4"]])
    config.DEAL_INBOX_DIR = inbox
    config.DEAL_DROP_DIR = tmp / "drop"
    (tmp / "drop" / "Read").mkdir(parents=True)
    (tmp / "drop" / "LOI Cedar Bend.pdf").write_bytes(b"%PDF-1.4")
    (tmp / "drop" / "Read" / "PSA already read.pdf").write_bytes(b"%PDF-1.4")

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
    check("the first Smartsheet read is the starting point, not news",
          not any(u["source"] == "Smartsheet" for u in rec.get("updates", [])), rec.get("updates"))

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
    checks = [c.rsplit("/", 1)[-1] for c in sale.get("checks", [])]
    check("a sale comes with papers to check its price against, never a draft, invoice or loan paper",
          "261016 Affidavit of Property Value RECORDED.pdf" in checks and "261009 PSA FE.pdf" in checks
          and not any(w in " ".join(checks).lower() for w in ("draft", "redline", "invoice", "deed of trust")), checks)

    section("Offer papers: one folder, newest paper")
    papers = [i for i in items if i["key"].startswith("paper:")]
    pd = {i["offer_folder"]: i["document"].rsplit("/", 1)[-1] for i in papers}
    check("the newest signed paper is picked in each offer folder",
          pd == {"2026 Granite Homes": "261009 PSA FE.pdf", "2026 Birch Builders": "261003 LOI Birch.pdf"}, pd)
    check("a PSA subfolder counts as its offer, not a buyer", "PSA" not in pd)

    section("Saved email attachments, and date rows from email and calendar")
    inbox_items = [i for i in items if i["key"].startswith(("inbox:", "row:"))]
    got = sorted(i["document"] for i in inbox_items)
    check("only deal papers and deal rows are offered: no accounting, no other attachment, no lunch, "
          "no email without a deal name, no private row",
          got == sorted(["Closing Statement Maple.pdf", "Oak Ridge 40 DD expiration", "Call with Sam",
                         "Oak Ridge 40 closing moved"]), got)
    check("an attachment is shown by its real name from the log, kept by its code name",
          any(i["key"].startswith("inbox:a1.pdf") and i["document"] == "Closing Statement Maple.pdf" for i in inbox_items))
    check("a calendar row needs only one word or name; an email row needs a word and a deal name",
          "Call with Sam" in got and "Sam says hi" not in got and "Team lunch confirm" not in got, got)
    check("rows are kept by their ID, by tab", {i["key"] for i in inbox_items if i["key"].startswith("row:")}
          == {"row:Calendar:c2", "row:Calendar:c3", "row:Email:e1"})
    check("no workbook is ever offered as an attachment", not any(d.endswith(".xlsx") for d in got), got)
    check("a private row is left out and its subject is not written into the team's record",
          "Oak Ridge 40 papers" not in got and "Oak Ridge 40 papers" not in json.dumps(rec))
    e1 = next((i for i in inbox_items if i["key"] == "row:Email:e1"), None)
    check("the email row's text is what Claude reads", e1 is not None and "Closing moved to Jan 9." in T.read_text(e1))
    check("a row's source says email or calendar event", e1 is not None and e1["source"] == "email"
          and next(i for i in inbox_items if i["key"] == "row:Calendar:c2")["source"] == "calendar event")
    check("asking twice gives the same rows, once each",
          sorted(i["key"] for i in T.pending(rec) if i["key"].startswith("row:")) == sorted(
              i["key"] for i in inbox_items if i["key"].startswith("row:")))

    section("Marking read")
    check("a closing can be marked read", T.mark_read(rec, sale["key"], "sale recorded"))
    granite = next(i for i in papers if i["offer_folder"] == "2026 Granite Homes")
    check("an offer paper can be marked read", T.mark_read(rec, granite["key"], "pipeline updated"))
    check("an inbox file can be marked read", all(T.mark_read(rec, i["key"], "recorded") for i in inbox_items))
    drop_items = [i for i in items if i["key"].startswith("drop:")]
    check("a paper dropped in by hand is offered; its Read folder is not",
          [i["document"] for i in drop_items] == ["LOI Cedar Bend.pdf"], [i["document"] for i in drop_items])
    check("a dropped paper is filed into Read once recorded",
          T.mark_read(rec, drop_items[0]["key"], "pipeline updated")
          and (config.DEAL_DROP_DIR / "Read" / "LOI Cedar Bend.pdf").is_file()
          and not (config.DEAL_DROP_DIR / "LOI Cedar Bend.pdf").exists())
    left = {i["key"] for i in T.pending(rec)}
    check("what was read is not offered again",
          not ({sale["key"], granite["key"], drop_items[0]["key"], *(i["key"] for i in inbox_items)} & left)
          and len(left) == 2, left)
    check("an unknown key is refused", not T.mark_read(rec, "drive:no such folder", "x"))

    section("Email files screened before Claude sees them")
    box = tmp / "inbox2"
    IB.make_workbooks(box)   # empty log: files with no log row are screened on their contents
    (box / "Attachments" / "Scan_0034.txt").write_text("Letter of Intent. The purchase price is $1. Due diligence 60 days.")
    (box / "Attachments" / "Q3 report.txt").write_text("Balance Sheet. Letter of intent, purchase price, due diligence.")
    (box / "Attachments" / "PSA Oak draft.txt").write_text("Purchase and sale agreement. Schedule K-1 attached.")
    (box / "Attachments" / "Holiday party.txt").write_text("Bring a dish.")
    config.DEAL_INBOX_DIR, saved_inbox = box, config.DEAL_INBOX_DIR
    got = [i["document"] for i in T.pending(rec) if i["key"].startswith("inbox:")]
    check("a deal paper with no deal word in its name, and no log row, is found by what it says",
          "Scan_0034.txt" in got, got)
    check("confidential text stops the read, even with deal words beside it", "Q3 report.txt" not in got, got)
    check("an ordinary attachment is left alone", "Holiday party.txt" not in got, got)
    check("a deal-named paper holding confidential text is not read, and a person is told by name",
          "PSA Oak draft.txt" not in got and any("PSA Oak draft.txt" in a for a in rec["attention"]), got)
    kept = json.dumps(rec)
    check("names of screened-out files are never written into the team's record",
          "Q3 report" not in kept and "Holiday party" not in kept)
    T.note_inbox(rec)
    check("a working email folder gives no warning", T.inbox_warning(rec, today) == "")
    check("a folder quiet for over 30 days warns that the flow may have stopped",
          "may have stopped" in T.inbox_warning(rec, today + _dt.timedelta(days=45)))
    (box / "Date Updates.xlsx").write_bytes(b"locked or half-synced")
    try:
        T.pending(rec)
        check("a workbook that will not open does not stop the tracker", True)
    except Exception as e:
        check("a workbook that will not open does not stop the tracker", False, e)
    T.note_inbox(rec)
    check("...and the page says date updates were not checked, never 'nothing new'",
          "could not be opened" in T.inbox_warning(rec, today), T.inbox_warning(rec, today))
    config.DEAL_INBOX_DIR = saved_inbox
    rec.pop("inbox_check")

    section("The word list keeps itself current")
    T.CALENDAR_WORDS_FILE.write_text(
        "# made-up word list\n# --- words (edit freely) ---\ndd\nclosing\n"
        "# --- deal, company and people names (kept current by the tracker's daily run; edits here are replaced) ---\n"
        "old name\n", encoding="utf-8")
    before = T.CALENDAR_WORDS_FILE.read_text(encoding="utf-8").split("# --- deal")[0]
    changed = T.write_calendar_words(rec)
    after = T.CALENDAR_WORDS_FILE.read_text(encoding="utf-8")
    words, deals, others = T.calendar_words()
    check("the words part is kept exactly", after.startswith(before) and words == ["dd", "closing"], words)
    check("the old single names part is replaced by deal names and other names",
          changed and "old name" not in after and "# --- deal names" in after and "# --- other names" in after)
    check("deal names: live pipeline deals and current properties, company words dropped",
          {"oak ridge 40", "maple draw", "pine flat 12"} <= set(deals), deals)
    check("a sold deal is not a deal name", "willow flat" not in deals, deals)
    check("other names: buyers and sellers on live deals only",
          {"granite homes", "smith family"} <= set(others) and "birch builders" not in others, others)
    check("nothing changes when nothing is new", T.write_calendar_words(rec) is False)

    section("Deal and Status go back to the inbox workbooks")
    T.CALENDAR_WORDS_FILE.write_text(
        "# --- words (edit freely) ---\ndd\nexpiration\nexpires\nclosing\nconfirm\nhearing\n"
        "# --- deal names (kept current by the tracker's daily run; edits here are replaced) ---\noak ridge 40\n"
        "# --- other names (kept current by the tracker's daily run; edits here are replaced) ---\nsam\n",
        encoding="utf-8")
    oak = next(d for d in rec["pipeline"] if d["property"] == "Oak Ridge 40")
    rec["processed"]["row:Email:e1"]["deal"] = oak["id"]
    add_rows(inbox / IB.DATES_BOOK, "Email", [
        ["Nov 18, 2026 9:00 AM", "A. Broker", "Oak Ridge 40 hearing moved", "Council hearing moved to Jan 5.", "", "", "New", "e5"],
        ["Nov 18, 2026 9:00 AM", "A. Broker", "Oak Ridge 40 expires soon", "DD expires Dec 1.", "", "", "New", "e6"]])
    e5 = next(i for i in T.pending(rec) if i["key"] == "row:Email:e5")
    q = T.needs_a_look(rec, e5, "a council hearing has no place in the tracker")
    st = T.inbox_status(rec)
    by_id = {r["id"]: r for part in ("attachments", "calendar", "email") for r in st[part]}
    check("a recorded row is Done, with its deal",
          by_id.get("e1", {}).get("status") == "Done" and by_id["e1"]["deal"] == "Oak Ridge 40", by_id.get("e1"))
    check("a recorded attachment is Done", by_id.get("a1", {}).get("status") == "Done", by_id.get("a1"))
    check("an attachment private by its name is Skipped", by_id.get("a2", {}).get("status") == "Skipped", by_id.get("a2"))
    check("a row with private text is Skipped", by_id.get("e4", {}).get("status") == "Skipped", by_id.get("e4"))
    check("an attachment or row that is not about a deal is Not a deal",
          all(by_id.get(i, {}).get("status") == "Not a deal" for i in ("a3", "c1", "e2", "e3")),
          {i: by_id.get(i) for i in ("a3", "c1", "e2", "e3")})
    check("a row the tracker could not settle is Needs a look",
          by_id.get("e5", {}).get("status") == "Needs a look", by_id.get("e5"))
    check("a row not looked at yet is New", by_id.get("e6", {}).get("status") == "New", by_id.get("e6"))
    check("each part lists only its own sheet's rows",
          {r["id"] for r in st["calendar"]} == {"c1", "c2", "c3"} and {r["id"] for r in st["attachments"]} == {"a1", "a2", "a3"})
    check("Needs a look asks the team once, naming the row and the reason",
          q in rec["attention"] and "Oak Ridge 40 hearing moved" in q and "council hearing" in q
          and rec["attention"].count(q) == 1, q)
    dumped = json.dumps(st)
    check("the status file holds IDs, deals and statuses only, never a subject or file name",
          not any(s in dumped for s in ("closing moved", "Maple", "K-1", "Lunch", "hearing")), dumped)
    T.INBOX_STATUS_FILE = T.RECORD_FILE.with_name("inbox status.json")
    check("the status file is written beside the record", T.write_inbox_status(rec)
          and json.loads(T.INBOX_STATUS_FILE.read_text(encoding="utf-8"))["email"])
    section("It learns from what a person decides")
    check("a lesson is kept", T.add_lesson(rec, "Council hearings go in the deal's next step.", "test")
          and rec["lessons"][0]["lesson"] == "Council hearings go in the deal's next step.")
    check("the same lesson twice, or a blank one, is not kept",
          not T.add_lesson(rec, "council hearings go in the deal's NEXT step.", "test") and not T.add_lesson(rec, "  ", "test")
          and len(rec["lessons"]) == 1)
    for n in range(60):
        T.add_lesson(rec, f"made-up lesson {n}", "test")
    check("at most 50 lessons, newest first", len(rec["lessons"]) == T.MAX_LESSONS
          and rec["lessons"][0]["lesson"] == "made-up lesson 59")
    check("lessons can be removed by their words", T.remove_lesson(rec, "made-up lesson") == T.MAX_LESSONS
          and rec["lessons"] == [])
    try:
        T.settle_question(rec, "no such question")
        check("settling a question that is not there is refused", False)
    except ValueError:
        check("settling a question that is not there is refused", True)
    said = T.settle_question(rec, "hearing moved", deal=oak["id"], fields={"next_step": "Council hearing Jan 5"},
                             lesson="Council hearings go in the deal's next step.", name_means_deal="Oak Ridge")
    st = T.inbox_status(rec)
    e5s = next(r for r in st["email"] if r["id"] == "e5")
    check("a settled question leaves Needs a look and its row becomes Done, with its deal",
          q not in rec["attention"] and e5s == {"id": "e5", "deal": "Oak Ridge 40", "status": "Done"}, (said, e5s))
    check("the answer is applied to the deal", oak.get("next_step") == "Council hearing Jan 5", oak.get("next_step"))
    check("the lesson is kept for next time", any("Council hearings" in x["lesson"] for x in rec["lessons"]))
    check("the name the person gave now means that deal, so it is not asked again",
          [d["id"] for d in P.find_entry(rec, "Oak Ridge")] == [oak["id"]]
          and [d["name"] for d in T.find_deal(rec, "Oak Ridge")] == ["Oak Ridge 40"])
    check("What changed names a calendar event or an email as its source",
          T.source_label(next(i for i in T.pending(rec) if i["key"] == "row:Email:e6")) == "Email Oak Ridge 40 expires soon"
          and T.source_label({"key": "row:Calendar:c9", "document": "DD due"}) == "Calendar event DD due"
          and T.source_label({"key": "inbox:a1.pdf|8", "document": "LOI.pdf"}) == "Email attachment LOI.pdf")

    section("Review fixes: the inbox never fails quietly")
    # A machine with no inbox must never blank the team's status file.
    T.INBOX_STATUS_FILE.write_text('{"keep": true}', encoding="utf-8")
    config.DEAL_INBOX_DIR, saved_inbox = None, config.DEAL_INBOX_DIR
    check("a machine with no email folder leaves the status file alone",
          T.write_inbox_status(rec) is False and "keep" in T.INBOX_STATUS_FILE.read_text(encoding="utf-8"))
    config.DEAL_INBOX_DIR = saved_inbox
    # A private subject (the do-not-open list) is never read, even in an email row.
    add_rows(inbox / IB.DATES_BOOK, "Email", [
        ["Nov 18, 2026 9:00 AM", "A. Title", "Wire instructions Oak Ridge 40 closing", "Account 123, closing Friday.", "", "", "New", "e7"]])
    pend = {i["key"] for i in T.pending(rec)}
    st = {r["id"]: r for r in T.inbox_status(rec)["email"]}
    check("an email row whose subject is on the do-not-open list is skipped and never offered",
          "row:Email:e7" not in pend and st.get("e7", {}).get("status") == "Skipped", st.get("e7"))
    # Needs a look works on any paper, not only inbox rows.
    try:
        q2 = T.needs_a_look(rec, {"key": "paper:Nowhere/!OFFERS/2026 Test", "document": "LOI test.pdf",
                                  "filed": "2026-11-01"}, "it names two deals")
        ok = q2 in rec["attention"]
        T.settle_question(rec, "it names two deals", lesson="")
        ok = ok and q2 not in rec["attention"]
    except Exception as e:
        ok = False
        q2 = e
    check("Needs a look on an offer paper asks the team and can be settled", ok, q2)
    # Old rows drop out of the status file, so the flow's daily work stays small.
    add_rows(inbox / IB.DATES_BOOK, "Email", [
        ["Sep 1, 2026 9:00 AM", "A. Broker", "Oak Ridge 40 closing old", "Closing moved.", "", "", "New", "e8"]])
    T.pending(rec)
    T.mark_read(rec, "row:Email:e8", "pipeline", oak["id"])
    rec["processed"]["row:Email:e8"]["at"] = "2026-09-01T09:00:00"
    ids = {r["id"] for r in T.inbox_status(rec)["email"]}
    check("a row settled long ago is left out of the status file; recent and waiting ones stay",
          "e8" not in ids and {"e1", "e6"} <= ids, ids)
    # A word list that cannot be saved never stops the run.
    blocker = tmp / "blocker"
    blocker.write_text("x")
    saved_words, T.CALENDAR_WORDS_FILE = T.CALENDAR_WORDS_FILE, blocker / "calendar words.txt"
    try:
        check("a word list that cannot be written is reported, not raised", T.write_calendar_words(rec) is False)
    except Exception as e:
        check("a word list that cannot be written is reported, not raised", False, e)
    T.CALENDAR_WORDS_FILE = saved_words
    # A missing workbook or tab says so; attachments without their log wait.
    box3 = tmp / "inbox3"
    (box3 / IB.ATTACH_DIR).mkdir(parents=True)
    (box3 / IB.ATTACH_DIR / "z9.txt").write_text("Letter of intent. Purchase price $1. Due diligence 30 days.")
    config.DEAL_INBOX_DIR = box3
    got3 = [i for i in T.pending(rec) if i["key"].startswith(("inbox:", "row:"))]
    T.note_inbox(rec)
    w = T.inbox_warning(rec, today)
    check("with no Attachments Log, attachments wait instead of being read without their names", got3 == [], got3)
    check("a missing workbook is named on the page, never 'nothing new'",
          "Date Updates" in w and "Attachments Log" in w, w)
    IB.make_workbooks(box3)
    wb = openpyxl.load_workbook(box3 / IB.DATES_BOOK)
    del wb["Email"]
    wb.save(box3 / IB.DATES_BOOK)
    T.note_inbox(rec)
    check("a missing tab is named on the page", "Email" in T.inbox_warning(rec, today), T.inbox_warning(rec, today))
    # A stopped email flow is noticed even though the status flow keeps touching the workbooks.
    wb = openpyxl.load_workbook(box3 / IB.DATES_BOOK)
    wb.create_sheet("Email")
    wb.save(box3 / IB.DATES_BOOK)
    for f in (box3 / IB.ATTACH_DIR).iterdir():
        old = time.time() - 60 * 86400
        os.utime(f, (old, old))
    T.note_inbox(rec)
    check("a quiet inbox warns even when the workbooks themselves were just saved",
          "may have stopped" in T.inbox_warning(rec, today), T.inbox_warning(rec, today))
    config.DEAL_INBOX_DIR = saved_inbox
    rec.pop("inbox_check", None)

    section("A held sale from an email, settled the right way")
    pine_d = T.new_deal(rec, "Held Sale Test 9", "check script")   # its own deal; removed below
    hkey = "inbox:hs1.pdf|8"
    T.record_event(rec, pine_d, "sale", "2026-11-10", 500_000, None, "Buyer Co", False, None, None,
                   "Email attachment HS closing.pdf")
    T.hold_for_question(rec, hkey, pine_d, "2026-11-10")
    check("a held sale's email row is Needs a look", T._status_of(rec, hkey)[0] == "Needs a look",
          T._status_of(rec, hkey))
    hq = rec.get("questions", {}).get(hkey, "")
    try:
        T.settle_question(rec, hq[:60] if hq else "no question was linked")
        check("settling a held sale's question is refused, so the sale is never dropped unrecorded", False,
              "settle_question accepted it")
    except ValueError as e:
        check("settling a held sale's question is refused, so the sale is never dropped unrecorded",
              bool(hq) and "confirm sale" in str(e) and hq in rec["attention"], e)
    T.confirm_sale(rec, pine_d, "2026-11-10")
    check("confirming the held sale also marks its email row Done",
          T._status_of(rec, hkey)[0] == "Done" and hkey not in rec.get("questions", {}), T._status_of(rec, hkey))
    rec["deals"].remove(pine_d)
    rec["updates"] = [u for u in rec.get("updates", []) if u.get("deal") != "Held Sale Test 9"]

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
    alone = T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes LLC", False, None, None, "SS 1")
    check("a sale with only one paper giving its price is held, not recorded",
          not o40["sales"] and o40["held_sales"] and "held" in alone[0]
          and any("Oak Ridge 40: sale on" in a and "only one paper" in a for a in rec["attention"]), alone)
    off = T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes LLC", False, None, None, "SS 1",
                         check_source="Affidavit of Value", check_price=640_000)
    check("a sale whose second paper disagrees is held and both prices are named",
          not o40["sales"] and len(o40["held_sales"]) == 1
          and any("Affidavit of Value gives $640,000" in a for a in rec["attention"]), off)
    chk = dict(check_source="261009 PSA FE.pdf", check_price=600_000)
    T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes LLC", False, None, None, "SS 1", **chk)
    check("a sale two papers agree on is recorded, naming the second paper",
          len(o40["sales"]) == 1 and o40["sales"][0]["checked_against"] == "261009 PSA FE.pdf", o40["sales"])
    again = T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Granite Homes", False, None, None, "SS 1 copy", **chk)
    T.record_event(rec, o40, "sale", "2026-10-15", 600_000, 5, "Birch Builders", False, None, None, "SS 2", **chk)
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
    check("a held final sale does not mark the deal exited", T.status(o80) != "Fully exited", T.status(o80))
    said = T.confirm_sale(rec, o80, "2026-10-20")
    check("a person confirming a held sale records it and clears its question",
          "recorded" in said and o80["sales"][-1]["checked_against"] == "confirmed by a person"
          and not o80["held_sales"] and not any(a.startswith("Oak Ridge 80: sale on") for a in rec["attention"]), said)
    check("a final sale reads fully exited", T.status(o80) == "Fully exited")
    check("an estimated statement is told apart from a final one",
          T.is_estimate("261010 Est SS.pdf") and T.is_estimate("240613 Est SS MVE SELLER EXECUTED.pdf")
          and not T.is_estimate("INST-Final Settlement Statement .PDF")
          and not T.is_estimate("261015 Settlement Statement Final.pdf") and not T.is_estimate("Estes Park SS.pdf"))

    section("Returns by period, and the 2008 recession")
    from deal_tracker import dashboard as D
    wf = D.deal_row(by["Willow Flat"], today)
    check("a lost deal shows as fully exited, never as lost money", wf["status"] == "Fully exited")
    ex = lambda last, put, back, irr: {"status": "Fully exited", "last_sale": last, "contributions": put,
                                       "distributions": back, "profit": back - put, "irr": irr}
    recent, old = (today - _dt.timedelta(days=400)).isoformat(), (today - _dt.timedelta(days=4000)).isoformat()
    ret = {x["years"]: x for x in D.returns([ex(recent, 100, 300, 0.4), ex(old, 100, 150, 0.1)], today)}
    check("cash multiple counts only deals sold in the period", ret[5]["deals"] == 1 and ret[5]["multiple"] == 3.0
          and ret[15]["deals"] == 2 and ret[15]["multiple"] == 2.25, ret)
    check("IRR is weighted by each deal's share of the profit", ret[None]["irr"] == round((200 * 0.4 + 50 * 0.1) / 250, 4),
          ret[None])
    rc = lambda a, last, status, marked=False: D._recession(_dt.date.fromisoformat(a) if a else None,
                                                            _dt.date.fromisoformat(last) if last else None, status, marked)
    check("held through 2008 counts as owned in the recession, by the dates",
          rc("2005-01-01", "2012-01-01", "Fully exited") is True and rc("2001-01-01", "2006-01-01", "Fully exited") is False
          and rc("2011-01-01", None, "Owned") is False and rc("2006-01-01", None, "Owned") is True)
    check("where the dates cannot tell, the old spreadsheet's mark is used, else not known",
          rc("2005-01-01", None, "Fully exited", True) is True and rc("2005-01-01", None, "Fully exited") is None)

    section("What changed, and why")
    portfolio.load_all_properties = lambda: (
        [{"name": "Oak Ridge 40", "category": "Final Engineering", "state": "Arizona"},
         {"name": "Cedar Bluff 40", "category": "Rezone", "state": "Arizona"},
         {"name": "Oak Ridge", "category": "Rezone", "state": "Arizona"}],
        [{"name": "Willow Flat", "state": "Colorado"}])
    T.refresh(rec)
    ups = rec.get("updates", [])
    check("a stage changed in Smartsheet is logged, old to new",
          any(u["deal"] == "Oak Ridge 40" and "Disposition → Final Engineering" in u["what"] for u in ups), ups[:3])
    maple_d = pl["Maple Draw"]
    old_price = maple_d.get("price")
    P.apply(rec, maple_d, {"price": 1_450_000, "close_date": "2026-12-01"}, "Offer paper 261020 Counter.pdf",
            "the seller countered at a higher price")
    u = rec["updates"][0]
    check("a paper's change is logged with the price and date, old to new, and the reason",
          u["deal"] == "Maple Draw" and "Price" in u["what"] and "$1,450,000" in u["what"] and "Dec 1, 2026" in u["what"]
          and (old_price is None or "→" in u["what"]) and u["why"] == "the seller countered at a higher price", u)
    n = len(rec["updates"])
    P.apply(rec, maple_d, {"price": 1_450_000}, "the same paper again")
    check("nothing is logged when nothing changed", len(rec["updates"]) == n)
    check("a closing statement's sale is logged",
          any(u["deal"] == "Oak Ridge 80" and u["what"].startswith("Sold the last of it") and "$900,000" in u["what"]
              for u in rec["updates"]), [u["what"] for u in rec["updates"][:5]])

    section("Smartsheet project dashboards")
    from deal_tracker import smartsheet_dashboards as SD
    cell = lambda label, v: {"label": label, "cell": {"displayValue": v}}
    sight = {"name": "7. Pine Flat 12 Dashboard", "widgets": [
        {"title": "Project Name", "contents": {"cellData": [cell("", "Pine Flat 12")]}},
        {"title": "Project Overview", "contents": {"cellData": [
            cell("Entity Name:", "Holdings VII, LLC"), cell("Investors:", "Made Up Investor A"),
            cell("City/State/County:", "Cottonwood/AZ/Yavapai"), cell("Cross-streets:", "Elm & 5th"),
            cell("Assessor Parcel No:", "100-01-001\n100-01-002"), cell("Zoning:", "R-1"),
            cell("Acreage:", "+/- 12.3"), cell("Lot Count:", "48")]}},
        {"title": "Current Phase Budget", "contents": {"cellData": [cell("Spent to Date", "$123,456")]}}]}
    got = SD.read_one(sight)
    check("a dashboard's investors and budget are never read",
          "Made Up Investor A" not in json.dumps(got) and "123,456" not in json.dumps(got), got)
    check("two parcel numbers stay apart", got.get("apn") == "100-01-001; 100-01-002", got.get("apn"))
    pine["lots"] = 50
    SD.apply(rec, [got])
    check("the dashboard fills the right deal, under the project's own name",
          pine["name"] == "Pine Flat 12" and pine["county"] == "Yavapai" and pine["zoning"] == "R-1"
          and "Pine Flat 12 Investors, LLC" in pine["aliases"] and "Holdings VII, LLC" in pine["aliases"], pine["name"])
    check("an acreage within rounding is left alone, a different lot count is asked about",
          pine["acres"] == 12.0 and pine["lots"] == 50 and any("lot count 48" in a for a in rec["attention"]),
          (pine["acres"], pine["lots"]))
    SD.apply(rec, [{**got, "zoning": "R-2"}])
    check("a rezoning on the dashboard shows in what changed",
          rec["updates"][0]["deal"] == "Pine Flat 12" and "Zoning R-1 → R-2" in rec["updates"][0]["what"], rec["updates"][0])
    check("a dashboard that fits no deal is asked about",
          SD.apply(rec, [{"dashboard": "9. Nowhere 5 Dashboard", "project": "Nowhere 5"}]) == 0
          and any("Nowhere 5" in a for a in rec["attention"]))

    section("View site links")
    from pipeline import property_coordinates as PCO
    real_load = PCO.load_coordinates
    PCO.load_coordinates = lambda _d: {
        "Aspen Draw": {"lat": 33.1, "lon": -111.1, "precision": "parcel"},
        "Birch Flat": {"lat": 33.2, "lon": -111.2, "precision": "city"}}
    try:
        rows = [{"name": "Aspen Draw – Lot 2", "aliases": []}, {"name": "Aspen Draw – Lot 4", "aliases": []},
                {"name": "Mission Flat - NEC of Elm & Birch Rd", "aliases": []}, {"name": "Birch Flat", "aliases": []}]
        DB.add_sites(rows, "name")
        check("two lots of one site both get its link", all(r.get("site") for r in rows[:2])
              and rows[0]["site"]["url"].endswith("33.1,-111.1"), rows[:2])
        check("a name holding the site's words apart gets no link", "site" not in rows[2], rows[2])
        check("a site known only to its town says so", rows[3].get("site", {}).get("rough") is True, rows[3])
    finally:
        PCO.load_coordinates = real_load

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
    ws.cell(mrow, X.COL["Days to Close"], 999)        # a worked-out column: must be ignored
    band = next(r for r in range(1, ws.max_row + 1) if str(ws.cell(r, 1).value or "").startswith("ADD NEW DEALS"))
    ws.cell(band + 1, X.COL["Type"], "Acquisition")
    ws.cell(band + 1, X.COL["Property"], "Aspen Mesa")
    hs = wb[X.HISTORY_SHEET]
    hr = next(r for r in range(1, 40) if hs.cell(r, 1).value == "Deal")
    hh = [c.value for c in hs[hr]]
    hrow = next(r for r in range(hr + 1, hs.max_row + 1) if hs.cell(r, hh.index(X.ID_HEAD) + 1).value == o80["id"])
    hs.cell(hrow, hh.index("Contributions") + 1, 3_100_000)
    oi = wb["Open Items"]
    oi.delete_rows(next(r for r in range(1, 40) if oi.cell(r, 1).value == X.ITEMS_HEAD) + 1)
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
    wb = openpyxl.load_workbook(xl)
    check("the tabs are the expected ones, with no About tab",
          wb.sheetnames == ["Dashboard", "Deal Detail", X.CHANGES_SHEET, "Offer Terms", "Open Items", X.HISTORY_SHEET, "Sales",
                            X.QUESTIONS_SHEET, "Lists"] and wb["Lists"].sheet_state == "hidden", wb.sheetnames)
    check("every tab a person sees says what it is and what can be changed",
          all(str(wb[n]["A2"].value or "").startswith("What this is:") and str(wb[n]["A3"].value or "").startswith("What you can do:")
              for n in wb.sheetnames if n not in ("Dashboard", "Lists")))
    check("the questions for a person are on their own tab",
          [c.value for c in wb[X.QUESTIONS_SHEET]["B"][4:]] == rec["attention"] and rec["attention"], rec["attention"])
    wb.close()

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
    // Home (owner, 2026-11-17): the progress chart, what changed, coming up and open items, and nothing
    // else; every sidebar entry is a page of its own, and the sidebar carries no counts.
    o.tabs = $$(".side .sh").length;
    o.homeFirst = $$("#snav-p button")[0].textContent.trim() === "Home";
    o.navNumbers = /\\d/.test($$(".side .navlist button").map(b => b.textContent).join(" ").replace(/2008|5 years/g, ""));
    o.homeShown = !$("#phome").hidden && $("#psecs").hidden;
    o.chart = $$("#progc svg").length;
    // The chart's totals and the tiles are worked out from DATA on every load, so a rebuilt page moves
    // with the record: recompute them here and compare, so nothing on Home can be a fixed figure.
    const past = DATA.deals.filter(d => d.status !== "Being acquired");
    const usd = v => Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v).toLocaleString("en-US")}`;
    const inv = past.reduce((a, d) => a + (d.contributions || 0), 0);
    const back = past.filter(d => d.status === "Fully exited").reduce((a, d) => a + (d.distributions || 0), 0);
    const labels = $$("#progc text.big").map(t => t.textContent);
    o.chartTotals = labels.includes(usd(inv) + " invested") && (!back || labels.includes(usd(back) + " returned"));
    o.heldTile = $$("#progk .kpi .val")[0].textContent === String(DATA.deals.filter(d => ["Owned", "Partially sold"].includes(d.status)).length);
    o.prows = $$("tr[data-pid]").length;
    o.kpis = $$("#pk .kpi").length;
    o.csub = $("#csub").textContent;
    o.open = $$("#open .item").length;
    o.changes = $$("#changes .ch").length;
    o.needsShown = !$("#needsbox").hidden;
    o.needs = $$("#needs .item").length;
    o.learned = $$("#learned .item").length;
    const pbtn = l => $$("#snav-p button").find(b => b.textContent.trim() === l);
    pbtn("Acquisitions").click();
    o.oneGroup = $$("#psecs details.sec").length;
    o.groupRows = $$("#psecs tr[data-pid]").length;
    o.homeHidden = $("#phome").hidden;
    pbtn("Home").click();
    o.backHome = !$("#phome").hidden && $("#psecs").hidden;
    // Past deals has no Home: each entry is its own page, and a years slider narrows the list.
    const hbtn = l => $$("#snav-h button").find(b => b.textContent.trim() === l);
    o.histFirst = $$("#snav-h button")[0].textContent.trim();
    hbtn("Fully exited").click();
    o.histShown = !$('section[data-view="history"]').hidden;
    o.hrows = $$("tr[data-id]").length;
    const yrs = $("#yrs"); yrs.value = 1; yrs.dispatchEvent(new Event("input"));
    o.hrows1 = $$("tr[data-id]").length;
    yrs.value = yrs.max; yrs.dispatchEvent(new Event("input"));
    hbtn("Returns").click();
    o.returns = !$("#hhome").hidden && $("#hlist").hidden && $$("#ret tr").length === 4;
    const q = $("#q"); q.value = "oak ridge"; q.dispatchEvent(new Event("input"));
    o.hits = $$("#results button").length;
    $("#results button").click();
    o.drawer = $("#drawer").classList.contains("on");
    o.dashes = /\\s[\\u2014\\u2013-]\\s/.test(document.body.innerText);
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
    # Its own profile: with the person's Edge already open, a shared one hangs the run.
    r = subprocess.run([str(edge), "--headless=new", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={page_path.parent / 'edge-profile'}",
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
    check("two sidebar lists, Home first and no counts; Home shows the progress chart, coming up and open items, no deal tables",
          o.get("tabs") == 2 and o.get("homeFirst") and o.get("navNumbers") is False and o.get("homeShown")
          and o.get("chart") == 1 and o.get("prows") == 0 and o.get("kpis") == 0 and o.get("csub") and o.get("open") == 1, o)
    check("Home's progress totals and tiles are worked out from the record, so they move with every rebuild",
          o.get("chartTotals") and o.get("heldTile"), o)
    check("a group in the sidebar opens a page with that group alone, and Home comes back",
          o.get("oneGroup") == 1 and o.get("groupRows", 0) >= 1 and o.get("homeHidden") and o.get("backHome"), o)
    check("Past deals has no Home; a status opens its own list", o.get("histFirst") not in (None, "Home")
          and o.get("histShown") and o.get("hrows") == 3, o)
    check("the years slider narrows the list", 0 < o.get("hrows1", 0) < o.get("hrows", 0), o)
    check("Returns shows the summary and the returns table on its own page", o.get("returns"), o)
    check("the search finds both tabs' deals and opens one", o.get("hits", 0) >= 3 and o.get("drawer"), o)
    check("no dash between words anywhere on the page", o.get("dashes") is False)
    check("what changed is shown on the page", o.get("changes", 0) >= 3, o.get("changes"))
    check("Needs a look is shown on the page, with the questions the tracker could not settle",
          o.get("needsShown") and o.get("needs", 0) >= 1, o)
    check("what the tracker has learned is shown under it", o.get("learned", 0) >= 1, o)


def add_rows(path: Path, sheet: str, values: list[list]) -> None:
    """Add rows to a sheet's table, as a flow's 'Add a row into a table' does."""
    wb = openpyxl.load_workbook(path)
    ws = wb[sheet]
    for v in values:
        ws.append(v)
    ws.tables[sheet].ref = f"A1:{openpyxl.utils.get_column_letter(ws.max_column)}{ws.max_row}"
    wb.save(path)


def inbox_workbook_check(tmp: Path) -> None:
    """The two workbooks Ava's flows write into: made right, read right (owner, 2026-11-17)."""
    from deal_tracker import inbox as IB
    section("Inbox workbooks")
    box = tmp / "wb_inbox"
    box.mkdir()
    made = IB.make_workbooks(box)
    check("both workbooks are made", sorted(p.name for p in made) == sorted([IB.ATTACH_LOG, IB.DATES_BOOK]), made)
    check("a second call makes nothing and overwrites nothing", IB.make_workbooks(box) == [])
    cols_ok, hidden_ok, tables_ok = True, True, True
    for book, sheets in ((IB.ATTACH_LOG, ["Attachments"]), (IB.DATES_BOOK, ["Calendar", "Email"])):
        wb = openpyxl.load_workbook(box / book)
        for s in sheets:
            ws = wb[s]
            head = [c.value for c in ws[1]]
            cols_ok &= head == IB.COLUMNS[s]
            id_col = openpyxl.utils.get_column_letter(len(IB.COLUMNS[s]))
            hidden_ok &= bool(ws.column_dimensions[id_col].hidden)
            tables_ok &= s in ws.tables
    check("each sheet has exactly the approved columns, then ID", cols_ok)
    check("the ID column is hidden", hidden_ok)
    check("each sheet holds a table named after it, for the flows to add rows to", tables_ok)

    def add(book, sheet, values):
        add_rows(box / book, sheet, [values])

    add(IB.ATTACH_LOG, "Attachments", ["Nov 17, 2026 1:00 PM", "A. Broker", "Oak LOI",
                                       '=HYPERLINK("https://x/a1.pdf","LOI Oak Ridge 40.pdf")', "", "New", "a1"])
    add(IB.ATTACH_LOG, "Attachments", ["Nov 17, 2026 1:05 PM", "A. Broker", "Maple", "Closing Statement Maple.pdf",
                                       "", "New", "a2"])
    add(IB.ATTACH_LOG, "Attachments", ["Nov 17, 2026 1:06 PM", "A. Broker", "No id", "stray.pdf", "", "New", ""])
    names = IB.attachment_names(box)
    check("the real name comes from a link formula or a plain value; a row with no ID is left out",
          names == {"a1": "LOI Oak Ridge 40.pdf", "a2": "Closing Statement Maple.pdf"}, names)
    add(IB.DATES_BOOK, "Calendar", ["Nov 17, 2026 2:00 PM", "T. Title", "Oak Ridge 40 DD expiration",
                                    "Dec 15, 2026 10:00 AM to 10:30 AM", "Due diligence expires Dec 15.",
                                    "dd, oak ridge 40", "", "New", "c1"])
    add(IB.DATES_BOOK, "Email", ["Nov 17, 2026 3:00 PM", "A. Broker", "Oak Ridge 40 closing moved",
                                 "Closing moved to Jan 9.", "closing, oak ridge 40", "", "New", "e1"])
    rows = IB.date_rows(box)
    check("calendar and email rows come back with their tab", sorted((r["tab"], r["ID"]) for r in rows)
          == [("Calendar", "c1"), ("Email", "e1")], rows)
    cal = next((r for r in rows if r["tab"] == "Calendar"), {})
    text = IB.row_text(cal) if cal else ""
    check("the text Claude reads holds the event, when, and details",
          all(s in text for s in ("Oak Ridge 40 DD expiration", "Dec 15, 2026 10:00 AM", "Due diligence expires")), text)
    check("a missing workbook reads as no rows", IB.rows(tmp / "nowhere", "Calendar") == [])
    (box / IB.DATES_BOOK).write_bytes(b"not a workbook")
    try:
        IB.date_rows(box)
        check("a workbook that cannot be opened says so", False, "no error raised")
    except IB.InboxUnreadable:
        check("a workbook that cannot be opened says so", True)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="deal_tracker_check_"))
    try:
        inbox_workbook_check(tmp)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check(f"the inbox workbook checks ran to the end ({type(e).__name__}: {e})", False)
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
