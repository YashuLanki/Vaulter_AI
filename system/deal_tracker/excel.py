"""
The deal tracker's Excel copy. It started laid out like the team's own pipeline
workbook (owner, 2026-10-29); on 2026-11-03 the owner asked for it to be easy
to follow by someone who does not know what is going on in it, so every tab now
opens with what it is and what may be changed there, columns carry plain names
and a note, related columns sit under one coloured band, worked-out columns are
grey, and the Dashboard says where everything is. The About tab is gone: what
it said now sits where it applies.

Deal Detail is the data. The Dashboard's counts, rows, key dates and days to
close are Excel formulas pointing at it, so a stage or date changed in Deal
Detail shows on the Dashboard at once, in Excel, with no refresh. Days to close
and the profit, multiple and yearly return of a closed sale are formulas too
("worked out" columns): edits to them are never read back.

It is two-way. `write` returns a snapshot of every editable cell it wrote;
`pull_edits` compares the saved file with that snapshot and applies only what a
person changed, so a document recorded since cannot be overwritten by a stale
cell. Each data row ends in its record id; a row with no id is a new deal.
Tables are found by their heading row, not a fixed row number, so the notes
above them can change freely.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from openpyxl.utils import get_column_letter

from deal_tracker import pipeline as P
from deal_tracker import tracker as T

ID_HEAD = "Row ID (leave as is)"
FONT = "Calibri"
SLATE, GOLD, BAND, SOFT, GREY, CALC = "394F50", "F7C240", "E2E8E8", "F2F4F4", "595959", "EDEDED"
HISTORY_SHEET, QUESTIONS_SHEET, ITEMS_HEAD = "Past Deals", "Questions for the Team", "Open item"
CHANGES_SHEET = "What Changed"

# Deal Detail, most-used first. A key of None is worked out by Excel.
DETAIL = [("Type", "type"), ("Property", "property"), ("Stage", "stage"), ("Next Step", "next_step"),
          ("Where It Stands", "status"), ("Seller", "seller"), ("Buyer", "buyer"),
          ("Price ($)", "price"), ("How the Price Works", "price_basis"), ("Deposit ($)", "deposit"),
          ("LOI Date", "loi_date"), ("Counter Date", "counter_date"), ("PSA Date", "psa_date"),
          ("Due Diligence Ends", "dd_expires"), ("Close Date", "close_date"), ("Days to Close", None),
          ("City", "city"), ("County", "county"), ("State", "state"), ("Acres", "acres"),
          ("Parcel / Lot", "parcel"), ("APN", "apn"), ("Broker", "broker"), ("Escrow / Title", "escrow"),
          ("Date We Bought It", "orig_acq_date"), ("What We Paid ($)", "orig_cost"),
          ("Profit ($)", None), ("Multiple", None), ("Yearly Return (IRR)", None),
          ("CF Out", None), ("CF In", None), ("CF Date 1", None), ("CF Date 2", None)]
COL = {h: i + 1 for i, (h, _) in enumerate(DETAIL)}           # heading -> column number
L = {h: get_column_letter(i) for h, i in COL.items()}
HELPERS = ("CF Out", "CF In", "CF Date 1", "CF Date 2")       # feed the IRR formula; hidden

GROUPS = [("THE DEAL", "Type", "Where It Stands"), ("WHO", "Seller", "Buyer"),
          ("MONEY", "Price ($)", "Deposit ($)"), ("KEY DATES", "LOI Date", "Days to Close"),
          ("THE LAND", "City", "APN"), ("WHO IS HELPING", "Broker", "Escrow / Title"),
          ("ONLY WHEN WE SELL LAND WE OWN", "Date We Bought It", "Yearly Return (IRR)")]

NOTES = {  # the note on each heading, for someone who has never seen the sheet
    "Type": "Acquisition = we are buying. Disposition = we are selling land we own.",
    "Property": "The property's name, as the team calls it.",
    "Stage": "How far along the deal is. Pick from the list: LOI Sent > Counter Sent > PSA Negotiation > "
             "Under Contract > Closing Scheduled > Closed. Terminated or Declined means it is off.",
    "Next Step": "What has to happen next, in a few words. Shown on the Dashboard.",
    "Where It Stands": "A sentence on the latest news, from the newest paper on file.",
    "Price ($)": "The price on the latest paper (letter of intent, counter offer or agreement).",
    "How the Price Works": "How the price is set, e.g. per acre or per lot.",
    "Deposit ($)": "Earnest money the buyer puts down.",
    "LOI Date": "Date of the letter of intent: the first written offer.",
    "Counter Date": "Date of the latest counter offer.",
    "PSA Date": "Date of the purchase and sale agreement: the contract.",
    "Due Diligence Ends": "Last day the buyer can study the land and still walk away.",
    "Close Date": "The day the sale is due to complete.",
    "Days to Close": "Worked out by Excel: days from today to the close date. Red means the date has passed. "
                     "Typing here is ignored.",
    "APN": "The county's parcel number.",
    "Escrow / Title": "The company holding the money and insuring the title.",
    "Date We Bought It": "Only for land we are selling: when we bought it.",
    "What We Paid ($)": "Only for land we are selling: what we paid for it.",
    "Profit ($)": "Worked out by Excel once a sale closes: sale price less what we paid, before costs.",
    "Multiple": "Worked out by Excel: sale price divided by what we paid. 2.0x = double.",
    "Yearly Return (IRR)": "Worked out by Excel: the yearly rate of return between buying and selling.",
}

HIST = [("Deal", "name", "name"), ("Status", "status", None), ("State", "state", "state"),
        ("Type", "type", "type"), ("Size", "size", "size"), ("Acres", "acres", "acres"),
        ("Bought (first money in)", "acquired", "acquired"), ("Purchase price", "purchase_price", "purchase_price"),
        ("Loan to buy it", "debt", "acquisition_debt"), ("Equity put in", "contributions", "contributions"),
        ("Returned", "distributions", "distributions"), ("Profit", "profit", None),
        ("Multiple", "multiple", None), ("Project IRR", "irr", "project_irr"),
        ("Last sale", "last_sale", "last_sale"), ("Years held", "hold", None),
        ("Smartsheet stage", "stage", None), ("Where", "location", None), ("Zoning", "zoning", None),
        ("Parcel numbers", "apn", None), ("Lots", "lots", None), ("Also called", "also", None),
        ("Old spreadsheet note", "excluded", "excluded")]
HIST_NOTES = {
    "Deal": "The deal's name. Click the arrow on any heading to sort or filter.",
    "Status": "Worked out: Owned, Partially sold, Fully exited, Being acquired or Lost. Typing here is ignored.",
    "Bought (first money in)": "When the first money went in. A year on its own means only the year is known.",
    "Loan to buy it": "Money borrowed to buy the land.",
    "Equity put in": "Money the investors put in.",
    "Returned": "Money paid back to the investors so far.",
    "Profit": "Worked out: returned less put in. Blank if either is missing.",
    "Multiple": "Worked out: returned divided by put in. 2.0 = doubled.",
    "Project IRR": "The yearly return, from the old spreadsheet.",
    "Years held": "Worked out: from first money in to the last sale, or to today if still held.",
    "Smartsheet stage": "Where Smartsheet says the project is. Change it in Smartsheet, not here.",
    "Also called": "Other names this deal goes by, such as its company's name. Read only.",
    "Where": "City, county and state, from the project's Smartsheet dashboard. Change it there, not here.",
    "Zoning": "From the project's Smartsheet dashboard. Change it there, not here.",
    "Parcel numbers": "The county's parcel numbers, from the project's Smartsheet dashboard.",
    "Lots": "Number of lots. Tell Claude if it is wrong.",
}

DATES = {"loi_date", "counter_date", "psa_date", "dd_expires", "close_date", "orig_acq_date", "acquired", "last_sale"}
MONEY = {"price", "deposit", "orig_cost", "purchase_price", "debt", "contributions", "distributions", "profit"}

SECTIONS = [  # Deal Detail and Dashboard sections: (title, what it holds, test)
    ("BUYING", "Land we are trying to buy, from first offer to closing.",
     lambda d: d.get("type") == "Acquisition" and d.get("stage") not in ("Terminated", "Declined", "Unconfirmed")),
    ("SELLING", "Land we own that a buyer is negotiating for or under contract on.",
     lambda d: d.get("type") == "Disposition" and d.get("stage") in P.ACTIVE),
    ("LISTED FOR SALE", "Land we own that is on the market with no buyer yet.",
     lambda d: d.get("type") == "Disposition" and d.get("stage") == "Listed"),
    ("SOLD", "Sales that have closed.",
     lambda d: d.get("type") == "Disposition" and d.get("stage") == "Closed"),
    ("NEEDS AN UPDATE", "Deals whose stage nobody has confirmed lately.",
     lambda d: d.get("stage") == "Unconfirmed"),
    ("FELL THROUGH", "Deals that ended without closing.",
     lambda d: d.get("stage") in ("Terminated", "Declined")),
    ("NO STAGE SET", "Deals with no stage set.", lambda d: True),
]
NEW_ROWS = 8
STAGE_COLOURS = {  # fill, font: as on the page, colour only where something went wrong
    "active": ("EEF1F0", "1D2627"), "contract": ("EEF1F0", "1D2627"), "closed": ("EEF1F0", "5F6C6D"),
    "dead": ("F9E3E0", "B03A2E"), "other": ("EEF1F0", "5F6C6D")}
STAGE_KIND = {"LOI Sent": "active", "LOI Received": "active", "Counter Sent": "active",
              "PSA Negotiation": "contract", "Under Contract": "contract", "Closing Scheduled": "contract",
              "Closed": "closed", "Terminated": "dead", "Declined": "dead", "Listed": "other", "Unconfirmed": "other"}
STATUS_COLOURS = {"Owned": "active", "Partially sold": "contract", "Fully exited": "closed",
                  "Lost": "dead", "Being acquired": "other"}


def norm(v):
    """One form for a cell, so what was written and what was read back compare equal."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, _dt.datetime):
        return v.date().isoformat()
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return " ".join(str(v).split())


def _cell(key, v):
    """A full ISO date becomes a real Excel date; everything else is written as it is."""
    if key in DATES and isinstance(v, str) and len(v) == 10:
        try:
            return _dt.date.fromisoformat(v)
        except ValueError:
            return v
    return v


# ─── Writing ──────────────────────────────────────────────────────────────────

def write(payload: dict, path: Path) -> dict:
    import openpyxl
    from openpyxl.comments import Comment
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as letter
    from openpyxl.worksheet.datavalidation import DataValidation

    fill = lambda c: PatternFill("solid", fgColor=c)
    f = lambda **k: Font(name=FONT, **k)
    thin = Border(bottom=Side(style="thin", color="D5DBDA"))
    wrap_top = Alignment(wrap_text=True, vertical="top")

    def head_row(ws, row, cols, height=33, notes=None, calc=()):
        for c in range(1, cols + 1):
            cell = ws.cell(row, c)
            grey = cell.value in calc
            cell.fill = fill("7F8C8D" if grey else SLATE)
            cell.font = f(bold=True, color="FFFFFF", italic=grey)
            cell.alignment = Alignment(wrap_text=True, vertical="center")
            if notes and cell.value in notes:
                cell.comment = Comment(notes[cell.value], "Deal Tracker", width=300, height=110)
        ws.row_dimensions[row].height = height

    def intro(ws, title, what, how):
        """Every tab opens with its name, what it is and what may be changed there."""
        ws["A1"] = title
        ws["A1"].font = f(size=18, bold=True, color=SLATE)
        ws.row_dimensions[1].height = 28
        for r, (lab, text) in enumerate((("What this is:", what), ("What you can do:", how)), 2):
            ws.cell(r, 1, f"{lab}  {text}").font = f(size=11, color=GREY)
            ws.cell(r, 1).alignment = Alignment(vertical="center")
            ws.row_dimensions[r].height = 18

    def stage_colours(ws, rng, first, kinds=STAGE_KIND):
        for stage, kind in kinds.items():
            bg, fg = STAGE_COLOURS[kind]
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f'{first}="{stage}"'],
                                                           fill=fill(bg), font=Font(name=FONT, bold=True, color=fg)))

    wb = openpyxl.Workbook()
    snap = {"pipeline": {}, "history": {}, "terms": {}, "open_items": list(payload["open_items"])}
    pipeline = payload["pipeline"]
    calc_heads = {h for h, k in DETAIL if k is None}

    # ── Deal Detail (built first: the Dashboard's formulas point at its rows) ──
    dd = wb.active
    dd.title = "Deal Detail"
    dash = wb.create_sheet("Dashboard", 0)
    intro(dd, "Deal Detail: every live deal, one row each",
          "The deals we are buying or selling right now. The Dashboard is built from this tab.",
          "Type over any white cell and save. Grey columns are worked out by Excel. Hover a heading for what "
          "it means. Add a deal in the yellow rows at the bottom.")
    band, hdr = 5, 6
    for title, a, b in GROUPS:
        dd.merge_cells(start_row=band, start_column=COL[a], end_row=band, end_column=COL[b])
        c = dd.cell(band, COL[a], title)
        c.fill, c.font, c.alignment = fill(BAND), f(bold=True, size=10, color=SLATE), Alignment(horizontal="center")
    for i, (h, _) in enumerate(DETAIL, 1):
        dd.cell(hdr, i, h)
    dd.cell(hdr, len(DETAIL) + 1, ID_HEAD)
    head_row(dd, hdr, len(DETAIL) + 1, 33, NOTES, calc_heads)

    row, placed, section_rows = hdr + 1, set(), {}
    for title, what, test in SECTIONS:
        members = [d for d in pipeline if d["id"] not in placed and test(d)]
        if not members:
            continue
        dd.cell(row, 1, f"{title}  ({len(members)})").font = f(size=12, bold=True, color=SLATE)
        dd.cell(row, COL["Stage"], what).font = f(size=10, italic=True, color=GREY)
        for c in range(1, len(DETAIL) + 2):
            dd.cell(row, c).fill = fill(BAND)
        dd.row_dimensions[row].height = 21.75
        row += 1
        section_rows[title] = (what, [])
        for d in members:
            placed.add(d["id"])
            _detail_row(dd, row, d, f)
            section_rows[title][1].append(row)
            snap["pipeline"][d["id"]] = {k: norm(d.get(k)) for _, k in DETAIL if k}
            row += 1
    dd.cell(row, 1, "ADD NEW DEALS BELOW: fill in Type and Property at least, and leave Row ID blank").font = f(size=12, bold=True)
    for c in range(1, len(DETAIL) + 2):
        dd.cell(row, c).fill = fill(GOLD)
    row += 1
    for r in range(row, row + NEW_ROWS):
        _formulas(dd, r)
        for c in range(1, len(DETAIL) + 2):
            dd.cell(r, c).font = f()
            dd.cell(r, c).fill = fill("FFFBEA")
    last = row + NEW_ROWS - 1
    first = hdr + 1
    for r in range(first, last + 1):        # worked-out cells look worked out
        for h in calc_heads:
            if dd.cell(r, COL[h]).fill.fgColor.rgb in (None, "00000000"):
                dd.cell(r, COL[h]).fill = fill(CALC)

    widths = {"Type": 12, "Property": 24, "Stage": 17, "Next Step": 30, "Where It Stands": 40, "Seller": 26,
              "Buyer": 26, "Price ($)": 13, "How the Price Works": 30, "City": 13, "County": 11, "State": 6,
              "Acres": 8, "Parcel / Lot": 22, "APN": 13, "Broker": 20, "Escrow / Title": 20,
              "LOI Date": 13, "Counter Date": 13, "PSA Date": 13, "Due Diligence Ends": 13, "Close Date": 13,
              "Date We Bought It": 13, "What We Paid ($)": 13, "Deposit ($)": 12}
    for h, c in COL.items():
        dd.column_dimensions[L[h]].width = widths.get(h, 11)
    for h in HELPERS:
        dd.column_dimensions[L[h]].hidden = True
    dd.column_dimensions[letter(len(DETAIL) + 1)].width = 12
    dd.freeze_panes = dd.cell(first, COL["Stage"]).coordinate
    dd.sheet_view.showGridLines = False
    stage_rng = f"{L['Stage']}{first}:{L['Stage']}{last}"
    stage_colours(dd, stage_rng, f"{L['Stage']}{first}")
    days = f"{L['Days to Close']}{first}:{L['Days to Close']}{last}"
    dd.conditional_formatting.add(days, FormulaRule(formula=[f'AND(ISNUMBER({L["Days to Close"]}{first}),{L["Days to Close"]}{first}<0)'],
                                                    font=Font(name=FONT, bold=True, color="B03A2E")))
    dd.conditional_formatting.add(days, FormulaRule(formula=[f'AND(ISNUMBER({L["Days to Close"]}{first}),{L["Days to Close"]}{first}<=30)'],
                                                    font=Font(name=FONT, bold=True, color="A05F00")))
    v_stage = DataValidation(type="list", formula1="Lists!$A$1:$A$11", allow_blank=True)
    v_type = DataValidation(type="list", formula1="Lists!$B$1:$B$2", allow_blank=True)
    dd.add_data_validation(v_stage)
    dd.add_data_validation(v_type)
    v_stage.add(stage_rng)
    v_type.add(f"A{first}:A{last}")

    # ── What Changed: newest first ──
    ch = wb.create_sheet(CHANGES_SHEET)
    intro(ch, "What Changed: every update to the tracker, newest first",
          "Each change the tracker made, from Smartsheet, offer papers, closing statements or saved emails, and why.",
          "Read only. If something here is wrong, tell Claude.")
    for i, h in enumerate(["Date", "Deal", "What changed", "Why", "Where it came from"], 1):
        ch.cell(4, i, h)
    head_row(ch, 4, 5, 21.75)
    for n, u in enumerate(payload.get("updates") or [], 5):
        for i, v in enumerate([_cell("acquired", u.get("date")), u.get("deal"), u.get("what"), u.get("why"), u.get("source")], 1):
            c = ch.cell(n, i, v)
            c.font, c.alignment = f(bold=(i == 2)), wrap_top
        ch.cell(n, 1).number_format = "mmm d, yyyy"
    if not payload.get("updates"):
        ch.cell(5, 2, "Nothing new yet.").font = f(italic=True, color=GREY)
    for col, w in zip("ABCDE", (13, 28, 60, 50, 36)):
        ch.column_dimensions[col].width = w
    ch.freeze_panes = "A5"

    # ── Offer Terms: read down each column ──
    terms = wb.create_sheet("Offer Terms")
    terms.sheet_view.showGridLines = False
    # The deals with an offer in play, as on the team's sheet: not every listing with nothing stated.
    live = [d for d in pipeline if d.get("stage") not in P.DONE and (d.get("terms") or d.get("stage") in P.ACTIVE)]
    intro(terms, "Offer Terms: what the latest paper on each deal says",
          "One column per deal with an offer in play, from the newest letter of intent, counter or agreement on file.",
          "Read down a column. 'Not stated' means the paper does not say. Type over a cell and save to correct it.")
    terms.cell(5, 1, "Deal")
    for j, d in enumerate(live, 2):
        terms.cell(5, j, d.get("property"))
        terms.column_dimensions[letter(j)].width = 28
        snap["terms"][d["id"]] = {t: norm(d.get("terms", {}).get(t)) for t in P.TERMS}
    head_row(terms, 5, len(live) + 1)
    term_notes = {"Document / Date": "Which paper this column comes from, and its date.",
                  "DD / Feasibility": "How long the buyer has to study the land and walk away.",
                  "Key Condition": "Anything the deal depends on, such as an approval."}
    for i, t in enumerate(P.TERMS, 6):
        lab = terms.cell(i, 1, t)
        lab.fill, lab.font, lab.alignment = fill(BAND), f(bold=True), wrap_top
        if t in term_notes:
            lab.comment = Comment(term_notes[t], "Deal Tracker", width=260, height=70)
        for j, d in enumerate(live, 2):
            c = terms.cell(i, j, d.get("terms", {}).get(t) or "Not stated")
            c.font, c.alignment, c.border = f(bold=(t == "Price"), color=None if d.get("terms", {}).get(t) else "8B9798"), wrap_top, thin
        terms.row_dimensions[i].height = 48 if t in ("Price", "Closing", "Key Condition") else 33.75
    idr = 6 + len(P.TERMS)
    terms.cell(idr, 1, ID_HEAD).font = f(size=9, color="8B9798")
    for j, d in enumerate(live, 2):
        terms.cell(idr, j, d["id"]).font = f(size=9, color="8B9798")
    terms.column_dimensions["A"].width = 20
    terms.freeze_panes = "B6"

    # ── Open Items ──
    items = wb.create_sheet("Open Items")
    intro(items, "Open Items: loose ends to chase",
          "Things on live deals that someone still has to sort out, such as a missing signature.",
          "Add a line for a new one. Delete the line once it is sorted, then save.")
    items.cell(4, 1, ITEMS_HEAD)
    head_row(items, 4, 1, 21.75)
    for n, i in enumerate(payload["open_items"], 5):
        items.cell(n, 1, i).alignment = wrap_top
    items.column_dimensions["A"].width = 140

    # ── Past Deals ──
    hist = wb.create_sheet(HISTORY_SHEET)
    intro(hist, "Past Deals: every deal the firm has done",
          "Each deal from the old spreadsheet and since, with what went in, what came back and how long it was held.",
          "Correct a white cell and save. Grey columns are worked out. Use the arrows on the headings to "
          "sort or filter, for example by Status.")
    hh = 4
    calc_hist = {h for h, _, fld in HIST if fld is None}
    for i, (h, _, _) in enumerate(HIST, 1):
        hist.cell(hh, i, h)
    hist.cell(hh, len(HIST) + 1, ID_HEAD)
    head_row(hist, hh, len(HIST) + 1, 33, HIST_NOTES, calc_hist)
    for n, d in enumerate(payload["deals"], hh + 1):
        vals = {k: (d[k][:4] if k in ("acquired", "last_sale") and d.get(k) and d.get(k + "_approx") else d.get(k))
                for _, k, _ in HIST if k != "also"}
        vals["also"] = "; ".join(d.get("aliases") or [])
        for i, (_, k, _) in enumerate(HIST, 1):
            hist.cell(n, i, _cell(k, vals[k]))
        hist.cell(n, len(HIST) + 1, d["id"])
        snap["history"][d["id"]] = {fld: norm(vals[k]) for _, k, fld in HIST if fld}
    _format_table(hist, [k for _, k, _ in HIST] + ["id"], f, hh)
    for i, (h, _, _) in enumerate(HIST, 1):
        if h in calc_hist:
            for r in range(hh + 1, hist.max_row + 1):
                hist.cell(r, i).fill = fill(CALC)
    for i, (h, k, _) in enumerate(HIST, 1):
        if k == "multiple":
            for r in range(hh + 1, hist.max_row + 1):
                hist.cell(r, i).number_format = '0.00"x"'
        if k == "irr":
            for r in range(hh + 1, hist.max_row + 1):
                hist.cell(r, i).number_format = "0.0%"
    st = letter([k for _, k, _ in HIST].index("status") + 1)
    stage_colours(hist, f"{st}{hh + 1}:{st}{max(hist.max_row, hh + 1)}", f"{st}{hh + 1}", STATUS_COLOURS)
    hist.freeze_panes = f"B{hh + 1}"

    # ── Sales ──
    sales = wb.create_sheet("Sales")
    intro(sales, "Sales: each closing, one row each",
          "Every sale recorded since the tracker started, part sales included, with where the figure came from.",
          "Read only. If a sale is wrong or missing, tell Claude.")
    sales_cols = ["Deal", "Date", "Price", "Acres", "Buyer", "Final sale?", "Where the figure came from"]
    for i, h in enumerate(sales_cols, 1):
        sales.cell(4, i, h)
    head_row(sales, 4, 7, 21.75)
    n = 5
    for d in payload["deals"]:
        for s in d["sales"]:
            for i, v in enumerate([d["name"], _cell("last_sale", s.get("date")), s.get("price"), s.get("acres"),
                                   s.get("other_party"), "Yes, the last of it" if s.get("final") else "Part sale",
                                   s.get("source")], 1):
                sales.cell(n, i, v)
            n += 1
    _format_table(sales, ["name", "last_sale", "price", "acres", "x", "x", "x"], f, 4)
    sales.freeze_panes = "B5"

    # ── Questions for the Team ──
    need = wb.create_sheet(QUESTIONS_SHEET)
    intro(need, "Questions for the Team: things the tracker could not decide itself",
          "When two names might be one deal, or two papers disagree, the tracker never guesses. It asks here.",
          "Read only. Tell Claude the answer, for example: \"Willow Flat is new, not in the old spreadsheet\".")
    for i, h in enumerate(["#", "Question"], 1):
        need.cell(4, i, h)
    head_row(need, 4, 2, 21.75)
    for n, a in enumerate(payload["attention"], 5):
        need.cell(n, 1, n - 4).alignment = wrap_top
        need.cell(n, 2, a).alignment = wrap_top
    if not payload["attention"]:
        need.cell(5, 2, "Nothing to answer right now.").font = f(italic=True, color=GREY)
    need.column_dimensions["A"].width = 5
    need.column_dimensions["B"].width = 140

    # ── Lists: the drop-down choices, hidden ──
    lists = wb.create_sheet("Lists")
    for i, s in enumerate(P.STAGES, 1):
        lists.cell(i, 1, s)
    lists.cell(1, 2, "Acquisition")
    lists.cell(2, 2, "Disposition")
    lists.sheet_state = "hidden"

    # ── Dashboard: built last, so it can count every tab ──
    _dashboard(dash, payload, section_rows, first, last, hh, f, fill, thin, wrap_top, stage_colours)

    for ws in wb.worksheets:
        for row_ in ws.iter_rows():
            for c in row_:
                if c.font is None or c.font.name != FONT:
                    c.font = Font(name=FONT, size=c.font.sz if c.font else 11, bold=c.font.b if c.font else False,
                                  italic=c.font.i if c.font else False, color=c.font.color if c.font else None)
    wb.active = 0
    wb.save(path)
    return snap


def _dashboard(dash, payload, section_rows, first, last, hh, f, fill, thin, wrap_top, stage_colours):
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Font

    dash.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHIJKLM", (2, 30, 24, 15, 8, 28, 17, 14, 36, 16, 10, 34, 2)):
        dash.column_dimensions[col].width = w
    for r in (1, 2, 3):
        for c in range(2, 13):
            dash.cell(r, c).fill = fill(SLATE)
    dash["B1"] = "VAULTER  |  Deal Tracker"
    dash["B1"].font = f(size=20, bold=True, color="FFFFFF")
    dash["B2"] = '="Today is "&TEXT(TODAY(),"mmmm d, yyyy")&".  Last updated from Smartsheet, the drive and saved emails: ' \
                 + payload.get("built_at", "") + '."'
    dash["B2"].font = f(size=11, color=GOLD)
    dash["B3"] = ("Every deal the firm is buying or selling, and every deal it has done. It updates itself; "
                  "you only need to type when something is wrong or missing.")
    dash["B3"].font = f(size=11, color="FFFFFF")
    dash.row_dimensions[1].height = 34

    rng = lambda h: f"'Deal Detail'!${L[h]}${first}:${L[h]}${last}"
    act = '{"LOI Sent","LOI Received","Counter Sent","PSA Negotiation","Under Contract","Closing Scheduled"}'
    con = '{"PSA Negotiation","Under Contract","Closing Scheduled"}'

    def tiles(top, title, specs):
        dash.cell(top, 2, title).font = f(size=14, bold=True, color=SLATE)
        for col, label, formula, explain in specs:
            nxt = chr(ord(col) + 1)
            for r in range(top + 1, top + 5):
                for c in (col, nxt):
                    dash[f"{c}{r}"].fill = fill(SOFT)
            dash[f"{col}{top + 1}"] = label
            dash[f"{col}{top + 1}"].font = f(size=10, bold=True, color=GREY)
            dash[f"{col}{top + 2}"] = formula
            dash[f"{col}{top + 2}"].font = f(size=22, bold=True, color=SLATE)
            dash[f"{col}{top + 2}"].alignment = Alignment(horizontal="left")
            dash[f"{col}{top + 3}"] = explain
            dash[f"{col}{top + 3}"].font = f(size=9, color=GREY)
            dash[f"{col}{top + 3}"].alignment = Alignment(wrap_text=True, vertical="top")
            dash.merge_cells(f"{col}{top + 3}:{nxt}{top + 4}")
            dash.merge_cells(f"{col}{top + 1}:{nxt}{top + 1}")   # room for a long label or total
            dash.merge_cells(f"{col}{top + 2}:{nxt}{top + 2}")
        dash.row_dimensions[top + 2].height = 34

    tiles(5, "DEALS IN PROGRESS", [
        ("B", "BUYING", f'=SUMPRODUCT(({rng("Type")}="Acquisition")*ISNUMBER(MATCH({rng("Stage")},{act},0)))',
         "Land we are trying to buy, from first offer to closing."),
        ("D", "SELLING", f'=SUMPRODUCT(({rng("Type")}="Disposition")*ISNUMBER(MATCH({rng("Stage")},{act},0)))',
         "Land we own that a buyer is negotiating for."),
        ("F", "UNDER CONTRACT", f'=SUMPRODUCT(ISNUMBER(MATCH({rng("Stage")},{con},0))*1)',
         "Buying or selling, with a contract being drafted or signed."),
        ("H", "CLOSING IN 30 DAYS", f'=SUMPRODUCT(ISNUMBER(MATCH({rng("Stage")},{act},0))*ISNUMBER({rng("Close Date")})'
                                    f'*({rng("Close Date")}>=TODAY())*({rng("Close Date")}<=TODAY()+30))',
         "Live deals due to complete within a month."),
        ("J", "NEXT CLOSING", f'=IF(COUNTIFS({rng("Close Date")},">="&TODAY(),{rng("Stage")},"<>Closed")=0,"None set",'
                              f'TEXT(_xlfn.MINIFS({rng("Close Date")},{rng("Close Date")},">="&TODAY(),{rng("Stage")},"<>Closed"),"mmm d, yyyy"))',
         "The soonest close date on a live deal."),
    ])
    dash["J7"].font = f(size=16, bold=True, color=SLATE)

    hs = lambda col: f"'{HISTORY_SHEET}'!${col}${hh + 1}:${col}$5000"
    status_col = get_column_letter([k for _, k, _ in HIST].index("status") + 1)
    put_col = get_column_letter([k for _, k, _ in HIST].index("contributions") + 1)
    back_col = get_column_letter([k for _, k, _ in HIST].index("distributions") + 1)
    tiles(11, "PAST DEALS AT A GLANCE", [
        ("B", "OWNED NOW", f'=COUNTIF({hs(status_col)},"Owned")+COUNTIF({hs(status_col)},"Partially sold")',
         "Deals we still hold, including ones partly sold."),
        ("D", "FULLY SOLD", f'=COUNTIF({hs(status_col)},"Fully exited")', "Deals we have completely sold."),
        ("F", "EQUITY PUT IN", f'=SUM({hs(put_col)})', "Investor money put into every deal, all time."),
        ("H", "RETURNED", f'=SUM({hs(back_col)})', "Money paid back to investors, all time."),
        ("J", "QUESTIONS", f"=COUNTA('{QUESTIONS_SHEET}'!B5:B500)-COUNTIF('{QUESTIONS_SHEET}'!B5:B500,\"Nothing*\")",
         "Things the tracker needs a person to answer. See the Questions tab."),
    ])
    for c in ("F13", "H13"):
        dash[c].number_format = '"$"#,##0.0,,"M"'

    # Where to find things: one line per tab, each a link.
    r = 17
    dash.cell(r, 2, "WHERE TO FIND THINGS").font = f(size=14, bold=True, color=SLATE)
    guide = [("Deal Detail", "Every live deal, every column. Change a stage, date or next step here."),
             (CHANGES_SHEET, "Every update, newest first, and why it happened."),
             ("Offer Terms", "What the latest offer or agreement on each deal says: price, deposit, dates, conditions."),
             ("Open Items", "Loose ends still to chase on live deals."),
             (HISTORY_SHEET, "Every deal the firm has done: money in, money back, years held."),
             ("Sales", "Each closing, part sales included."),
             (QUESTIONS_SHEET, "Things the tracker could not decide itself. Tell Claude the answers.")]
    for name, what in guide:
        r += 1
        c = dash.cell(r, 2, name)
        c.hyperlink, c.font = f"#'{name}'!A1", f(bold=True, underline="single", color="3D5F86")
        dash.cell(r, 3, what).font = f(color=GREY)
    r += 1
    dash.cell(r, 2, "Hover over a column heading on any tab to see what it means. Grey columns are worked out "
                    "by Excel. Red dates have passed.").font = f(size=10, italic=True, color=GREY)
    r += 2

    cols = ["Property", "Parcel / Lot", "Location", "Acres", "Buyer or Seller", "Stage", "Price", "How the Price Works",
            "Key Date", "Days to Close", "Next Step"]
    d_ = lambda h, x: f"'Deal Detail'!{L[h]}{x}"
    for title, (what, rows) in section_rows.items():
        dash.cell(r, 2, f"{title}  ({len(rows)})").font = f(size=13, bold=True, color=SLATE)
        dash.cell(r, 4, what).font = f(size=10, italic=True, color=GREY)
        dash.row_dimensions[r].height = 21.75
        r += 1
        for i, h in enumerate(cols, 2):
            dash.cell(r, i, h)
        for c in range(2, 13):
            cell = dash.cell(r, c)
            cell.fill, cell.font = fill(SLATE), f(bold=True, color="FFFFFF")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        dash.row_dimensions[r].height = 30
        r += 1
        top = r
        for x in rows:
            date_txt = lambda h, lab: (f'IF({d_(h, x)}<>"","{lab} "&IFERROR(TEXT({d_(h, x)},"mmm d, yyyy"),{d_(h, x)}),')
            cells = [
                f'={d_("Property", x)}&""', f'={d_("Parcel / Lot", x)}&""',
                f'=TRIM({d_("City", x)}&IF(AND({d_("City", x)}<>"",{d_("State", x)}<>""),", ","")&{d_("State", x)})',
                f'=IF({d_("Acres", x)}="","",{d_("Acres", x)})',
                f'=IF({d_("Type", x)}="Disposition",{d_("Buyer", x)},{d_("Seller", x)})&""',
                f'={d_("Stage", x)}&""',
                f'=IF({d_("Price ($)", x)}="","",{d_("Price ($)", x)})', f'={d_("How the Price Works", x)}&""',
                "=" + f'IF({d_("Close Date", x)}<>"",IF({d_("Stage", x)}="Closed","Closed ","Closing ")&IFERROR(TEXT({d_("Close Date", x)},"mmm d, yyyy"),{d_("Close Date", x)}),'
                + date_txt("Due Diligence Ends", "Due diligence ends") + date_txt("PSA Date", "Contract") + date_txt("Counter Date", "Counter")
                + date_txt("LOI Date", "First offer") + '"")))))',   # five IFs, five closing brackets
                f'=IF({d_("Days to Close", x)}="","",{d_("Days to Close", x)})',
                f'=IF({d_("Next Step", x)}="",{d_("Where It Stands", x)},{d_("Next Step", x)})&""',
            ]
            for i, v in enumerate(cells, 2):
                cell = dash.cell(r, i, v)
                cell.font, cell.alignment, cell.border = f(), wrap_top, thin
            dash.cell(r, 2).font = f(bold=True)
            dash.cell(r, 8).number_format = '"$"#,##0'
            dash.cell(r, 5).number_format = "0.00"
            dash.row_dimensions[r].height = 30   # two lines, so a price basis or next step is not cut off
            r += 1
        stage_colours(dash, f"G{top}:G{r - 1}", f"G{top}")
        dash.conditional_formatting.add(f"K{top}:K{r - 1}", FormulaRule(formula=[f'AND(ISNUMBER(K{top}),K{top}<0)'],
                                                                         font=Font(name=FONT, bold=True, color="B03A2E")))
        r += 1
    dash.cell(r, 2, "OPEN ITEMS").font = f(size=13, bold=True, color=SLATE)
    dash.cell(r, 4, "Loose ends still to chase. Change them on the Open Items tab.").font = f(size=10, italic=True, color=GREY)
    r += 1
    for i in range(max(len(payload["open_items"]), 1) + 5):
        a = dash.cell(r, 2, f"=IF('Open Items'!A{i + 5}=\"\",\"\",'Open Items'!A{i + 5})")
        a.font, a.alignment = f(), wrap_top
        dash.merge_cells(start_row=r, start_column=2, end_row=r, end_column=12)
        r += 1
    dash.freeze_panes = "A4"


def _detail_row(ws, r, d, f):
    from openpyxl.styles import Alignment, Border, Side
    for h, k in DETAIL:
        if k is None:
            continue
        c = ws.cell(r, COL[h], _cell(k, d.get(k)))
        c.font, c.alignment = f(), Alignment(wrap_text=True, vertical="top")
        c.border = Border(bottom=Side(style="thin", color="D5DBDA"))
    ws.cell(r, COL["Property"]).font = f(bold=True)
    ws.cell(r, len(DETAIL) + 1, d["id"]).font = f(size=9, color="8B9798")
    _formulas(ws, r)
    ws.row_dimensions[r].height = 48


def _formulas(ws, r):
    """Days to close, and the closed-sale figures, as in the team workbook."""
    g = lambda h: f"{L[h]}{r}"
    done = (f'AND({g("Stage")}="Closed",{g("Type")}="Disposition",ISNUMBER({g("What We Paid ($)")}),'
            f'{g("What We Paid ($)")}>0,ISNUMBER({g("Price ($)")}),ISNUMBER({g("Date We Bought It")}),ISNUMBER({g("Close Date")}))')
    ws[g("Days to Close")] = f'=IF(OR({g("Close Date")}="",{g("Stage")}="Closed"),"",IFERROR({g("Close Date")}-TODAY(),""))'
    ws[g("Profit ($)")] = f'=IF({done},{g("Price ($)")}-{g("What We Paid ($)")},"")'
    ws[g("Multiple")] = f'=IF({done},{g("Price ($)")}/{g("What We Paid ($)")},"")'
    ws[g("Yearly Return (IRR)")] = f'=IFERROR(IF({done},XIRR({g("CF Out")}:{g("CF In")},{g("CF Date 1")}:{g("CF Date 2")}),""),"")'
    ws[g("CF Out")] = f'=IF({done},-{g("What We Paid ($)")},"")'
    ws[g("CF In")] = f'=IF({done},{g("Price ($)")},"")'
    ws[g("CF Date 1")] = f'=IF({done},{g("Date We Bought It")},"")'
    ws[g("CF Date 2")] = f'=IF({done},{g("Close Date")},"")'
    for h, fmt in (("Price ($)", '"$"#,##0'), ("Deposit ($)", '"$"#,##0'), ("What We Paid ($)", '"$"#,##0'),
                   ("Profit ($)", '"$"#,##0'), ("Multiple", '0.00"x"'), ("Yearly Return (IRR)", "0.0%"),
                   ("CF Out", '"$"#,##0'), ("CF In", '"$"#,##0'), ("Days to Close", "0"), ("Acres", "0.00"),
                   ("CF Date 1", "mmm d, yyyy"), ("CF Date 2", "mmm d, yyyy")):
        ws[g(h)].number_format = fmt
    for h in ("LOI Date", "Counter Date", "PSA Date", "Due Diligence Ends", "Close Date", "Date We Bought It"):
        ws[g(h)].number_format = "mmm d, yyyy"


def _format_table(ws, keys, f, head):
    """A table whose headings sit on row `head`: widths, number formats and filter arrows."""
    from openpyxl.styles import Alignment
    for i, k in enumerate(keys, 1):
        col = ws.cell(head, i).column_letter
        cells = [ws.cell(r, i) for r in range(head, ws.max_row + 1)]
        width = max(10, min(44, max(len(str(c.value or "")) for c in cells[:80]) + 2))
        if k in MONEY or k in DATES:
            width = max(width, 15)   # "$-4,357,949" and "Sep 18, 2026" are wider than the raw value
        ws.column_dimensions[col].width = width
        for c in cells[1:]:
            c.font = f(size=10, color="8B9798") if str(ws.cell(head, i).value) == ID_HEAD else f()
            if k in DATES and isinstance(c.value, _dt.date):
                c.number_format = "mmm d, yyyy"
            elif k in MONEY and isinstance(c.value, (int, float)):
                c.number_format = '"$"#,##0'
            c.alignment = Alignment(vertical="top")
    ws.auto_filter.ref = f"A{head}:{get_column_letter(len(keys))}{max(ws.max_row, head)}"
    ws.sheet_view.showGridLines = False


# ─── Reading edits back ───────────────────────────────────────────────────────

def pull_edits(rec: dict, book: Path) -> list[str]:
    """What a person changed in the saved copy since it was written, applied to the record."""
    snap = rec.get("excel_snapshot")
    if not book.is_file() or not snap:
        return []
    mtime = book.stat().st_mtime
    if abs(mtime - rec.get("excel_mtime", 0)) < 1:
        return []
    import openpyxl
    try:
        wb = openpyxl.load_workbook(book, data_only=True)
    except Exception as e:
        T._note(rec, f"The Excel copy could not be read back ({e}); edits saved in it were not picked up.")
        return []
    said, src = [], "Edited in Excel"
    rec["excel_mtime"] = mtime

    def table(name, first_cell):
        if name not in wb.sheetnames:
            return [], []
        rows = list(wb[name].iter_rows(values_only=True))
        at = next((i for i, r in enumerate(rows) if r and str(r[0] or "").strip() == first_cell), None)
        return ([str(h or "").strip() for h in rows[at]], rows[at + 1:]) if at is not None else ([], [])

    head, body = table("Deal Detail", "Type")
    if ID_HEAD in head:
        col = {h: i for i, h in enumerate(head)}
        by_id = {d["id"]: d for d in rec.get("pipeline") or []}
        for r in body:
            get = lambda h: norm(r[col[h]]) if h in col and col[h] < len(r) else None
            vals = {k: get(h) for h, k in DETAIL if k}
            rid = get(ID_HEAD)
            if rid in by_id and rid in snap.get("pipeline", {}):
                d, old = by_id[rid], snap["pipeline"][rid]
                for k, v in vals.items():
                    if v != old.get(k):
                        d[k] = v
                        d.setdefault("sources", {})[k] = src
                        said.append(f"{d.get('property')}: {k.replace('_', ' ')}")
            elif not rid and vals.get("property") and str(vals.get("type") or "").title() in ("Acquisition", "Disposition"):
                d = P.new_entry(rec, str(vals["type"]), str(vals["property"]), src)
                P.apply(rec, d, {k: v for k, v in vals.items() if k not in ("type", "property")}, src)
                said.append(f"{d['property']}: added")

    if "Offer Terms" in wb.sheetnames:
        rows = {str(r[0] or "").strip(): r[1:] for r in wb["Offer Terms"].iter_rows(values_only=True) if r and r[0]}
        by_id = {d["id"]: d for d in rec.get("pipeline") or []}
        for j, rid in enumerate(rows.get(ID_HEAD, ())):
            if rid not in by_id or rid not in snap.get("terms", {}):
                continue
            d, old = by_id[rid], snap["terms"][rid]
            for t in P.TERMS:
                v = norm(rows[t][j]) if t in rows and j < len(rows[t]) else None
                v = None if v == "Not stated" else v
                if v != old.get(t):
                    terms = d.setdefault("terms", {})
                    terms.pop(t, None) if v is None else terms.__setitem__(t, str(v))
                    said.append(f"{d.get('property')}: offer terms, {t}")

    head, body = table("Open Items", ITEMS_HEAD)
    if head:
        now = [str(norm(r[0])) for r in body if r and norm(r[0]) is not None]
        if now != snap.get("open_items", []):
            rec["open_items"] = now
            said.append("open items")

    head, body = table(HISTORY_SHEET, "Deal")
    if ID_HEAD in head:
        col = {h: i for i, h in enumerate(head)}
        by_id = {d["id"]: d for d in rec["deals"]}
        for r in body:
            get = lambda h: norm(r[col[h]]) if h in col and col[h] < len(r) else None
            rid = get(ID_HEAD)
            vals = {fld: get(h) for h, _, fld in HIST if fld}
            if rid in by_id and rid in snap.get("history", {}):
                d, old = by_id[rid], snap["history"][rid]
                for fld, v in vals.items():
                    if v != old.get(fld):
                        if fld == "name" and v:
                            T.link_name(rec, d["name"], d)
                        d[fld] = v
                        d["sources"][fld] = src
                        said.append(f"{d['name']}: {fld.replace('_', ' ')}")
            elif not rid and vals.get("name"):
                d = T.new_deal(rec, str(vals["name"]), src, **{k: v for k, v in vals.items() if k != "name"})
                said.append(f"{d['name']}: added")
    wb.close()
    return said
