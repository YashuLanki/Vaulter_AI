"""
The deal tracker's Excel copy, laid out like the team's own pipeline workbook
(owner, 2026-10-29: "setup the excel like the excel that I sent"): a Dashboard
of live formulas over a Deal Detail sheet, Offer Terms read down each column,
and the history beside them.

Deal Detail is the data. The Dashboard's counts, rows, key dates and days out
are Excel formulas pointing at it, as in the team's workbook, so a stage or
date changed in Deal Detail shows on the Dashboard at once, in Excel, with no
refresh. Days out and the gross profit, multiple and IRR of a closed sale are
formulas too ("worked out" columns): edits to them are never read back.

It is two-way. `write` returns a snapshot of every editable cell it wrote;
`pull_edits` compares the saved file with that snapshot and applies only what a
person changed, so a document recorded since cannot be overwritten by a stale
cell. Each data row ends in its record id; a row with no id is a new deal.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

from openpyxl.utils import get_column_letter

from deal_tracker import pipeline as P
from deal_tracker import tracker as T

ID_HEAD = "Row ID (leave as is)"
FONT = "Times New Roman"
SLATE, GOLD, BAND, SOFT, GREY = "394F50", "F7C240", "E2E8E8", "F2F4F4", "595959"

# Deal Detail: the team workbook's 33 columns, in its order. A key of None is worked out.
DETAIL = [("Type", "type"), ("Property", "property"), ("Parcel / Lot", "parcel"), ("APN", "apn"),
          ("City", "city"), ("County", "county"), ("State", "state"), ("Acres", "acres"),
          ("Seller", "seller"), ("Buyer", "buyer"), ("Stage", "stage"), ("Status", "status"),
          ("LOI Date", "loi_date"), ("Counter Date", "counter_date"), ("PSA Date", "psa_date"),
          ("DD Expires", "dd_expires"), ("Close Date", "close_date"), ("Days Out", None),
          ("Price ($)", "price"), ("Price Basis", "price_basis"), ("Deposit ($)", "deposit"),
          ("Broker", "broker"), ("Escrow / Title", "escrow"), ("Next Step", "next_step"),
          ("Orig. Acq. Date", "orig_acq_date"), ("Orig. Cost ($)", "orig_cost"),
          ("Gross Profit ($)", None), ("Gross Multiple", None), ("Gross IRR", None),
          ("CF Out", None), ("CF In", None), ("CF Date 1", None), ("CF Date 2", None)]
COL = {h: i + 1 for i, (h, _) in enumerate(DETAIL)}           # heading -> column number
L = {h: get_column_letter(i) for h, i in COL.items()}

HIST = [("Deal", "name", "name"), ("State", "state", "state"), ("Type", "type", "type"), ("Size", "size", "size"),
        ("Acres", "acres", "acres"), ("Status (worked out)", "status", None),
        ("Smartsheet stage (worked out)", "stage", None),
        ("Bought (first money in)", "acquired", "acquired"), ("Purchase price", "purchase_price", "purchase_price"),
        ("Acquisition debt", "debt", "acquisition_debt"), ("Equity put in", "contributions", "contributions"),
        ("Returned", "distributions", "distributions"), ("Profit (worked out)", "profit", None),
        ("Multiple (worked out)", "multiple", None), ("Project IRR", "irr", "project_irr"),
        ("Last sale", "last_sale", "last_sale"), ("Hold, years (worked out)", "hold", None),
        ("Old tracker note", "excluded", "excluded")]

DATES = {"loi_date", "counter_date", "psa_date", "dd_expires", "close_date", "orig_acq_date", "acquired", "last_sale"}
MONEY = {"price", "deposit", "orig_cost", "purchase_price", "debt", "contributions", "distributions", "profit"}

SECTIONS = [  # Deal Detail and Dashboard sections, in the team workbook's order
    ("ACQUISITIONS", lambda d: d.get("type") == "Acquisition" and d.get("stage") not in ("Terminated", "Declined", "Unconfirmed")),
    ("DISPOSITIONS · ACTIVE", lambda d: d.get("type") == "Disposition" and d.get("stage") in P.ACTIVE),
    ("DISPOSITIONS · LISTED", lambda d: d.get("type") == "Disposition" and d.get("stage") == "Listed"),
    ("DISPOSITIONS · CLOSED", lambda d: d.get("type") == "Disposition" and d.get("stage") == "Closed"),
    ("STATUS UNCONFIRMED", lambda d: d.get("stage") == "Unconfirmed"),
    ("TERMINATED / DECLINED", lambda d: d.get("stage") in ("Terminated", "Declined")),
    ("OTHER", lambda d: True),
]
NEW_ROWS = 8
STAGE_COLOURS = {  # fill, font: the page's pill colours
    "active": ("E5ECF5", "3D5F86"), "contract": ("FDF3D6", "6B4D00"), "closed": ("E3F2E8", "2F7A4F"),
    "dead": ("F9E3E0", "B03A2E"), "other": ("EEF1F0", "5F6C6D")}
STAGE_KIND = {"LOI Sent": "active", "LOI Received": "active", "Counter Sent": "active",
              "PSA Negotiation": "contract", "Under Contract": "contract", "Closing Scheduled": "contract",
              "Closed": "closed", "Terminated": "dead", "Declined": "dead", "Listed": "other", "Unconfirmed": "other"}


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
    from openpyxl.formatting.rule import FormulaRule
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter as letter
    from openpyxl.worksheet.datavalidation import DataValidation

    fill = lambda c: PatternFill("solid", fgColor=c)
    f = lambda **k: Font(name=FONT, **k)
    thin = Border(bottom=Side(style="thin", color="D5DBDA"))
    wrap_top = Alignment(wrap_text=True, vertical="top")

    def head_row(ws, row, cols, height=21.75):
        for c in range(1, cols + 1):
            cell = ws.cell(row, c)
            cell.fill, cell.font = fill(SLATE), f(bold=True, color="FFFFFF")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.row_dimensions[row].height = height

    def stage_colours(ws, rng, first):
        for stage, kind in STAGE_KIND.items():
            bg, fg = STAGE_COLOURS[kind]
            ws.conditional_formatting.add(rng, FormulaRule(formula=[f'{first}="{stage}"'],
                                                           fill=fill(bg), font=Font(name=FONT, bold=True, color=fg)))

    wb = openpyxl.Workbook()
    snap = {"pipeline": {}, "history": {}, "terms": {}, "open_items": list(payload["open_items"])}
    pipeline = payload["pipeline"]

    # ── Deal Detail (built first: the Dashboard's formulas point at its rows) ──
    dd = wb.active
    dd.title = "Deal Detail"
    dash = wb.create_sheet("Dashboard", 0)
    dd["A1"] = "Deal Detail"
    dd["A1"].font = f(size=16, bold=True, color=SLATE)
    dd["A2"] = ("One row per deal. Change a stage, date or next step here and save: the Dashboard updates at "
                "once, and Deal Tracker.html the next time the tracker is opened through Claude.")
    dd["A2"].font = f(size=10, color=GREY)
    hdr = 3
    for i, (h, _) in enumerate(DETAIL, 1):
        dd.cell(hdr, i, h)
    dd.cell(hdr, len(DETAIL) + 1, ID_HEAD)
    head_row(dd, hdr, len(DETAIL) + 1, 31.5)

    row, placed, section_rows = hdr + 1, set(), {}
    for title, test in SECTIONS:
        members = [d for d in pipeline if d["id"] not in placed and test(d)]
        if not members:
            continue
        dd.cell(row, 1, title).font = f(size=12, bold=True)
        for c in range(1, len(DETAIL) + 2):
            dd.cell(row, c).fill = fill(BAND)
        dd.row_dimensions[row].height = 21.75
        row += 1
        section_rows[title] = []
        for d in members:
            placed.add(d["id"])
            _detail_row(dd, row, d, f)
            section_rows[title].append(row)
            snap["pipeline"][d["id"]] = {k: norm(d.get(k)) for _, k in DETAIL if k}
            row += 1
    dd.cell(row, 1, "ADD NEW DEALS BELOW (fill Type and Property; leave Row ID blank)").font = f(size=12, bold=True)
    for c in range(1, len(DETAIL) + 2):
        dd.cell(row, c).fill = fill(SOFT)
    row += 1
    new_first = row
    for r in range(row, row + NEW_ROWS):
        _formulas(dd, r)
        for c in range(1, len(DETAIL) + 2):
            dd.cell(r, c).font = f()
    last = row + NEW_ROWS - 1

    first = hdr + 1
    widths = {"Type": 12, "Property": 26, "Parcel / Lot": 24, "APN": 13, "City": 12, "County": 11, "State": 6,
              "Acres": 9, "Seller": 28, "Buyer": 28, "Stage": 17, "Status": 40, "Price Basis": 32, "Next Step": 30,
              "Broker": 22, "Escrow / Title": 20}
    for h, c in COL.items():
        dd.column_dimensions[L[h]].width = widths.get(h, 12)
    dd.column_dimensions[letter(len(DETAIL) + 1)].width = 12
    dd.freeze_panes = "C4"
    dd.sheet_view.showGridLines = False
    stage_rng = f"{L['Stage']}{first}:{L['Stage']}{last}"
    stage_colours(dd, stage_rng, f"{L['Stage']}{first}")
    days = f"{L['Days Out']}{first}:{L['Days Out']}{last}"
    dd.conditional_formatting.add(days, FormulaRule(formula=[f'AND(ISNUMBER({L["Days Out"]}{first}),{L["Days Out"]}{first}<0)'],
                                                    font=Font(name=FONT, bold=True, color="B03A2E")))
    dd.conditional_formatting.add(days, FormulaRule(formula=[f'AND(ISNUMBER({L["Days Out"]}{first}),{L["Days Out"]}{first}<=30)'],
                                                    font=Font(name=FONT, bold=True, color="A05F00")))
    v_stage = DataValidation(type="list", formula1="Lists!$A$1:$A$11", allow_blank=True)
    v_type = DataValidation(type="list", formula1="Lists!$B$1:$B$2", allow_blank=True)
    dd.add_data_validation(v_stage)
    dd.add_data_validation(v_type)
    v_stage.add(stage_rng)
    v_type.add(f"A{first}:A{last}")

    # ── Dashboard: formulas over Deal Detail, laid out like the team's ──
    dash.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFGHIJKLM", (2, 34, 26, 15, 8, 30, 17, 16, 40, 15, 9, 34, 2)):
        dash.column_dimensions[col].width = w
    for c in range(2, 13):
        dash.cell(1, c).fill = dash.cell(2, c).fill = fill(SLATE)
    dash["B1"] = "VAULTER  |  Acquisition & Disposition Tracker"
    dash["B1"].font = f(size=18, bold=True, color="FFFFFF")
    dash["B2"] = '="As of "&TEXT(TODAY(),"mmmm d, yyyy")'
    dash["B2"].font = f(size=11, color=GOLD)
    dash.row_dimensions[1].height, dash.row_dimensions[2].height = 31.5, 19.5

    rng = lambda h: f"'Deal Detail'!${L[h]}${first}:${L[h]}${last}"
    act = '{"LOI Sent","LOI Received","Counter Sent","PSA Negotiation","Under Contract","Closing Scheduled"}'
    con = '{"PSA Negotiation","Under Contract","Closing Scheduled"}'
    kpis = [
        ("B", "ACTIVE ACQUISITIONS", f'=SUMPRODUCT(({rng("Type")}="Acquisition")*ISNUMBER(MATCH({rng("Stage")},{act},0)))'),
        ("D", "ACTIVE DISPOSITIONS", f'=SUMPRODUCT(({rng("Type")}="Disposition")*ISNUMBER(MATCH({rng("Stage")},{act},0)))'),
        ("F", "UNDER CONTRACT / PSA", f'=SUMPRODUCT(ISNUMBER(MATCH({rng("Stage")},{con},0))*1)'),
        ("H", "CLOSINGS IN NEXT 30 DAYS", f'=SUMPRODUCT(ISNUMBER(MATCH({rng("Stage")},{act},0))*ISNUMBER({rng("Close Date")})'
                                          f'*({rng("Close Date")}>=TODAY())*({rng("Close Date")}<=TODAY()+30))'),
        ("J", "NEXT CLOSING", f'=IF(COUNTIFS({rng("Close Date")},">="&TODAY(),{rng("Stage")},"<>Closed")=0,"None scheduled",'
                              f'TEXT(_xlfn.MINIFS({rng("Close Date")},{rng("Close Date")},">="&TODAY(),{rng("Stage")},"<>Closed"),"mm/dd/yyyy"))'),
    ]
    for col, label, formula in kpis:
        nxt = chr(ord(col) + (2 if col == "J" else 1))
        for r in (4, 5, 6, 7):
            for c in (col, nxt):
                dash[f"{c}{r}"].fill = fill(SOFT)
        dash[f"{col}5"] = label
        dash[f"{col}5"].font = f(size=9, bold=True, color=GREY)
        dash[f"{col}6"] = formula
        dash[f"{col}6"].font = f(size=20 if col != "J" else 16, bold=True, color=SLATE)
        dash[f"{col}6"].alignment = Alignment(horizontal="left")
    dash.row_dimensions[6].height = 36

    r = 9
    cols = ["Property", "Parcel / Lot", "Location", "Acres", "Counterparty", "Stage", "Price", "Price Basis",
            "Key Date", "Days Out", "Next Step / Status"]
    d_ = lambda h, x: f"'Deal Detail'!{L[h]}{x}"
    for title, rows in section_rows.items():
        dash.cell(r, 2, title).font = f(size=13, bold=True, color=SLATE)
        dash.row_dimensions[r].height = 21.75
        r += 1
        for i, h in enumerate(cols, 2):
            dash.cell(r, i, h)
        for c in range(2, 13):
            cell = dash.cell(r, c)
            cell.fill, cell.font = fill(SLATE), f(bold=True, color="FFFFFF")
        r += 1
        top = r
        for x in rows:
            date_txt = lambda h, lab: (f'IF({d_(h, x)}<>"","{lab} "&IFERROR(TEXT({d_(h, x)},"mm/dd/yy"),{d_(h, x)}),')
            cells = [
                f'={d_("Property", x)}&""', f'={d_("Parcel / Lot", x)}&""',
                f'=TRIM({d_("City", x)}&IF(AND({d_("City", x)}<>"",{d_("State", x)}<>""),", ","")&{d_("State", x)})',
                f'=IF({d_("Acres", x)}="","",{d_("Acres", x)})',
                f'=IF({d_("Type", x)}="Disposition",{d_("Buyer", x)},{d_("Seller", x)})&""',
                f'={d_("Stage", x)}&""',
                f'=IF({d_("Price ($)", x)}="","",{d_("Price ($)", x)})', f'={d_("Price Basis", x)}&""',
                "=" + f'IF({d_("Close Date", x)}<>"",IF({d_("Stage", x)}="Closed","Closed ","Close ")&IFERROR(TEXT({d_("Close Date", x)},"mm/dd/yy"),{d_("Close Date", x)}),'
                + date_txt("DD Expires", "DD ends") + date_txt("PSA Date", "PSA") + date_txt("Counter Date", "Counter")
                + date_txt("LOI Date", "LOI") + '"")))))',   # five IFs, five closing brackets
                f'=IF({d_("Days Out", x)}="","",{d_("Days Out", x)})',
                f'=IF({d_("Next Step", x)}="",{d_("Status", x)},{d_("Next Step", x)})&""',
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
    r += 1
    for i in range(max(len(payload["open_items"]), 1) + 5):
        a = dash.cell(r, 2, f"=IF('Open Items'!A{i + 2}=\"\",\"\",'Open Items'!A{i + 2})")
        a.font, a.alignment = f(), wrap_top
        dash.merge_cells(start_row=r, start_column=2, end_row=r, end_column=12)
        r += 1
    dash.freeze_panes = "A8"

    # ── Offer Terms: read down each column ──
    terms = wb.create_sheet("Offer Terms")
    terms.sheet_view.showGridLines = False
    # The deals with an offer in play, as on the team's sheet: not every listing with nothing stated.
    live = [d for d in pipeline if d.get("stage") not in P.DONE and (d.get("terms") or d.get("stage") in P.ACTIVE)]
    terms["A1"] = "Offer Terms · Latest Document on File"
    terms["A1"].font = f(size=16, bold=True, color=SLATE)
    terms["A2"] = "Read down each column. 'Not stated' = not in the document. Edit a cell and save to change it."
    terms["A2"].font = f(size=11, color=GREY)
    terms.cell(4, 1, "Deal")
    for j, d in enumerate(live, 2):
        terms.cell(4, j, d.get("property"))
        terms.column_dimensions[letter(j)].width = 26
        snap["terms"][d["id"]] = {t: norm(d.get("terms", {}).get(t)) for t in P.TERMS}
    head_row(terms, 4, len(live) + 1)
    for i, t in enumerate(P.TERMS, 5):
        lab = terms.cell(i, 1, t)
        lab.fill, lab.font, lab.alignment = fill(BAND), f(bold=True), wrap_top
        for j, d in enumerate(live, 2):
            c = terms.cell(i, j, d.get("terms", {}).get(t) or "Not stated")
            c.font, c.alignment, c.border = f(bold=(t == "Price")), wrap_top, thin
        terms.row_dimensions[i].height = 48 if t in ("Price", "Closing", "Key Condition") else 33.75
    idr = 5 + len(P.TERMS)
    terms.cell(idr, 1, ID_HEAD).font = f(size=9, color="8B9798")
    for j, d in enumerate(live, 2):
        terms.cell(idr, j, d["id"]).font = f(size=9, color="8B9798")
    terms.column_dimensions["A"].width = 18
    terms.freeze_panes = "B5"

    # ── Open Items, History, Sales, Needs a Person, Lists, About ──
    items = wb.create_sheet("Open Items")
    items["A1"] = "Open item (add a line, or delete one that is resolved)"
    head_row(items, 1, 1)
    for i in payload["open_items"]:
        items.append([i])
    items.column_dimensions["A"].width = 130

    hist = wb.create_sheet("History")
    hist.append([h for h, _, _ in HIST] + [ID_HEAD])
    head_row(hist, 1, len(HIST) + 1, 31.5)
    for d in payload["deals"]:
        vals = {k: (d[k][:4] if k in ("acquired", "last_sale") and d.get(k) and d.get(k + "_approx") else d.get(k))
                for _, k, _ in HIST}
        hist.append([_cell(k, vals[k]) for _, k, _ in HIST] + [d["id"]])
        snap["history"][d["id"]] = {fld: norm(vals[k]) for _, k, fld in HIST if fld}
    _format_table(hist, [k for _, k, _ in HIST], f)
    hist.freeze_panes = "B2"

    sales = wb.create_sheet("Sales")
    sales.append(["Deal", "Date", "Price", "Acres", "Buyer", "Final sale", "Where from"])
    head_row(sales, 1, 7)
    for d in payload["deals"]:
        for s in d["sales"]:
            sales.append([d["name"], _cell("last_sale", s.get("date")), s.get("price"), s.get("acres"),
                          s.get("other_party"), "yes" if s.get("final") else "", s.get("source")])
    _format_table(sales, ["name", "last_sale", "price", "acres", "x", "x", "x"], f)
    sales.freeze_panes = "B2"

    need = wb.create_sheet("Needs a Person")
    need["A1"] = "Needs a person (tell Claude the answer)"
    head_row(need, 1, 1)
    for a in payload["attention"]:
        need.append([a])
    need.column_dimensions["A"].width = 130

    lists = wb.create_sheet("Lists")
    for i, s in enumerate(P.STAGES, 1):
        lists.cell(i, 1, s)
    lists.cell(1, 2, "Acquisition")
    lists.cell(2, 2, "Disposition")

    about = wb.create_sheet("About")
    for line in [f"Built {payload['built_at']}. This file and Deal Tracker.html update each other.",
                 "Edit Deal Detail, Offer Terms, Open Items and History and save. The Dashboard follows Deal Detail at "
                 "once; the page catches up the next time anyone opens the deal tracker through Claude.",
                 "Days Out, Gross Profit, Gross Multiple, Gross IRR and the CF columns, and columns marked (worked out), "
                 "are calculated; edits to them are ignored. Sales and Needs a Person are read only: tell Claude instead.",
                 "Leave the Row ID column as it is. A new row with no Row ID is added as a new deal.",
                 f"History from {payload['imported_from']}. New activity comes from Smartsheet, the property summaries, "
                 "closing statements and offer papers on the drive, and saved email attachments."]:
        about.append([line])
    about.column_dimensions["A"].width = 130

    for ws in wb.worksheets:
        for row_ in ws.iter_rows():
            for c in row_:
                if c.font is None or c.font.name != FONT:
                    c.font = Font(name=FONT, size=c.font.sz if c.font else 11, bold=c.font.b if c.font else False,
                                  color=c.font.color if c.font else None)
    wb.save(path)
    return snap


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
    """Days out, and the closed-sale figures, as in the team workbook."""
    g = lambda h: f"{L[h]}{r}"
    done = (f'AND({g("Stage")}="Closed",{g("Type")}="Disposition",ISNUMBER({g("Orig. Cost ($)")}),'
            f'{g("Orig. Cost ($)")}>0,ISNUMBER({g("Price ($)")}),ISNUMBER({g("Orig. Acq. Date")}),ISNUMBER({g("Close Date")}))')
    ws[g("Days Out")] = f'=IF(OR({g("Close Date")}="",{g("Stage")}="Closed"),"",IFERROR({g("Close Date")}-TODAY(),""))'
    ws[g("Gross Profit ($)")] = f'=IF({done},{g("Price ($)")}-{g("Orig. Cost ($)")},"")'
    ws[g("Gross Multiple")] = f'=IF({done},{g("Price ($)")}/{g("Orig. Cost ($)")},"")'
    ws[g("Gross IRR")] = f'=IFERROR(IF({done},XIRR({g("CF Out")}:{g("CF In")},{g("CF Date 1")}:{g("CF Date 2")}),""),"")'
    ws[g("CF Out")] = f'=IF({done},-{g("Orig. Cost ($)")},"")'
    ws[g("CF In")] = f'=IF({done},{g("Price ($)")},"")'
    ws[g("CF Date 1")] = f'=IF({done},{g("Orig. Acq. Date")},"")'
    ws[g("CF Date 2")] = f'=IF({done},{g("Close Date")},"")'
    for h, fmt in (("Price ($)", '"$"#,##0'), ("Deposit ($)", '"$"#,##0'), ("Orig. Cost ($)", '"$"#,##0'),
                   ("Gross Profit ($)", '"$"#,##0'), ("Gross Multiple", '0.00"x"'), ("Gross IRR", "0.0%"),
                   ("CF Out", '"$"#,##0'), ("CF In", '"$"#,##0'), ("Days Out", "0"), ("Acres", "0.00"),
                   ("CF Date 1", "mm/dd/yyyy"), ("CF Date 2", "mm/dd/yyyy")):
        ws[g(h)].number_format = fmt
    for h in ("LOI Date", "Counter Date", "PSA Date", "DD Expires", "Close Date", "Orig. Acq. Date"):
        ws[g(h)].number_format = "mm/dd/yyyy"


def _format_table(ws, keys, f):
    from openpyxl.styles import Alignment
    for i, k in enumerate(keys, 1):
        col = ws.cell(1, i).column_letter
        width = max(10, min(44, max(len(str(c.value or "")) for c in list(ws[col])[:80])))
        ws.column_dimensions[col].width = width
        for c in list(ws[col])[1:]:
            c.font = f(size=10, color="8B9798") if str(ws.cell(1, i).value) == ID_HEAD else f()
            if k in DATES and isinstance(c.value, _dt.date):
                c.number_format = "mmm d, yyyy"
            elif k in MONEY and isinstance(c.value, (int, float)):
                c.number_format = '"$"#,##0'
            c.alignment = Alignment(vertical="top")
    ws.auto_filter.ref = ws.dimensions
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

    if "Open Items" in wb.sheetnames:
        now = [str(norm(r[0])) for r in list(wb["Open Items"].iter_rows(values_only=True))[1:]
               if r and norm(r[0]) is not None]
        if now != snap.get("open_items", []):
            rec["open_items"] = now
            said.append("open items")

    head, body = table("History", "Deal")
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
