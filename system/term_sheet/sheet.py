"""
Mini term sheets, drafted from a property's due diligence report.

Built 2026-10-08 at the team's request: an analyst condenses a long DD report
into the firm's one-page "Mini Term Sheet" workbook by hand, and the Overview
tab is the same fourteen facts every time. Only the Overview tab is filled for
now; the other four tabs come across with their headings and stay blank until
the team decides how to fill them.

How the work is split, and why:

  * This module does what must not be left to judgement: finding the report,
    reading it on this machine, choosing which passages to send, finding the
    title / purchase / settlement documents by NAME, and writing the workbook
    without losing the template's formatting.
  * Claude does the writing, in the conversation, modelled on the reviewed
    reference sheet -- which is the team's answer key for wording.

Token cost is the design constraint. The first test report read whole is about
tens of thousands of tokens once Word's formatting is turned into
text. This sends the passages each field needs instead, typically a tenth of
that, using the same word-matching ranking as find_in_document.

Nothing real lives in this file. The reviewed example and the template are
the workbook in MINI_TERM_SHEETS_DIR/_reference/, in the team folder; this
repository is public.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# The Overview tab's labels, in row order, exactly as the template prints them.
OVERVIEW_FIELDS = [
    "Property / Project Name", "Owner", "Seller Contact", "Location", "County",
    "APN(s)", "Legal Description", "Parcel Size (Acres)", "Lot Size",
    "Current Zoning", "Proposed Use(s)", "Purchase Price Total",
    "Price per Acre", "Price per Lot",
]

# What to look for in the report for each field. Several fields share
# passages (acreage feeds three of them), so results are de-duplicated.
_FIELD_QUERIES = {
    "Owner": "owner vested record title entity partnership acquisition entity LLC LP",
    "Seller Contact": "seller grantor acquired from purchased from broker contact phone email",
    "Location": "property address location frontage intersection adjacent site",
    "County / APN(s)": "county APN parcel ID assessor property ID CAD",
    "Legal Description": "legal description abstract survey volume page deed records save except plat",
    "Parcel Size / Lot Size": "acreage acres gross net parcel survey lots lot size",
    "Current Zoning": "current zoning district rezoning required",
    "Proposed Use(s)": "proposed use development strategy scenario product program concept mixed-use "
                       "commercial-led PD retail hospitality BTR townhome multifamily",
    # Not "asking price": in the first blind test that pulled the comps
    # tables, while the purchase basis sat under "acquired ... for $".
    "Price": "acquired purchased purchase price basis paid closed contract sales price offer",
}

# Where a report usually keeps its fact table. Sent whole (capped), because a
# label/value table is exactly what the Overview is built from and word
# matching splits it badly.
# Most specific first. On the first test report "3. PROPERTY OVERVIEW" came
# before the real "Property Summary" table and, matching first, was sent in its
# place -- a short introduction, with the table's figures left to word search.
_FACT_SECTIONS = [
    re.compile(r"(property|site|deal) summary|key (facts|terms)|fact sheet", re.I),
    re.compile(r"property (identification|description)|legal identity", re.I),
    re.compile(r"(property|site) overview", re.I),
]

_OPENING = re.compile(r"executive summary|introduction|summary of findings", re.I)
_OPENING_CAP = 3000
_PASSAGES_PER_FIELD = 3
_FACT_SECTION_CAP = 7000
_TOTAL_CAP = 26000

# Names a Vaulter DD report goes by on the drive, for example
# "<Name> - DD Report - V2 - <date>.docx" and "...Due Diligence Report...".
_DD_NAME = re.compile(r"\b(dd|due[ _-]*diligence)[ _-]*report", re.I)
_DD_EXT = {".docx", ".pdf"}
# Folders holding someone ELSE's report (a seller's or a buyer's package) or a
# superseded one. Kept out of the candidate list and counted, never silently.
_NOT_OURS = re.compile(r"seller materials|!offers|archive|superseded|old versions", re.I)

# The documents that fill what a DD report usually leaves out: who owns it,
# who sold it, and what was paid. Matched against the file name and the
# folder holding it, in the firm's own short forms ("Commit", "SS", "Cert
# Status"). Whole words only, or "ss" matches every "assignment".
_W = r"(?<![a-z0-9])"
_E = r"(?![a-z0-9])"
_SUPPORTING = [
    ("Title commitment", re.compile(rf"{_W}(title )?commit(ment)?s?{_E}|title (report|policy)")),
    ("Purchase agreement", re.compile(rf"{_W}psa{_E}|purchase (and sale |& sale )?(agreement|contract)|"
                                      rf"unimproved property contract|sales contract")),
    ("Settlement statement", re.compile(rf"{_W}(ss|hud(-1)?){_E}|settlement st|closing statement|"
                                        rf"borrower'?s?[ _]st(ate)?m(en)?t")),
    ("Letter of intent", re.compile(rf"{_W}loi{_E}|letter of intent")),
    ("Entity status certificate", re.compile(rf"cert(ificate)?\.? (of )?(status|fact|good standing)|"
                                             rf"good standing|certificate of formation")),
    # Who may sign and who the limited partners are: the reviewed reference sheet
    # took both from a 2026 resolution the first version never looked for.
    ("Partnership resolution or consent", re.compile(rf"{_W}reso(lution)?s?{_E}|{_W}consent{_E}|"
                                                     rf"written consent|unanimous consent")),
    ("Partnership / operating agreement", re.compile(r"(partnership|operating|lllp|lp) agreement|"
                                                     r"agreement of limited partnership|"
                                                     r"amendment (to )?(the )?(pa|partnership|lllp|lp)(?![a-z0-9])")),
    ("Deed", re.compile(rf"{_W}(special |general )?warranty deed|{_W}grant deed|{_W}deed{_E}")),
    # Parcel numbers and the owner of record: the pending-deal test had to hunt
    # for the county treasurer's printouts that held all five parcel numbers.
    ("County tax records", re.compile(rf"treasurer|assessor|appraisal district|{_W}cad{_E}|"
                                      rf"{_W}tax(es)? ?(bill|statement|record|cert|printout)|{_W}taxes{_E}")),
    # The firm's own statement of its plan, which counts as the chosen strategy
    # for Proposed Use(s); a broker's memorandum (below) does not.
    ("Firm investment materials", re.compile(r"equity (offering|deck|raise)|investor (deck|presentation)|"
                                             r"investment (memo|committee)|\bic memo\b|pitch deck")),
    ("Offering memorandum / broker package", re.compile(rf"offering memorandum|{_W}om{_E}|brochure|flyer")),
    # For the Due Diligence tab (2026-10-12): the studies its questions ask about.
    ("Reports and studies", re.compile(r"phase (i|1|ii|2)\b|\besa\b|environmental|geotech|soils|traffic|"
                                       r"hec-ras|drainage study|flood study|wetland|alta|topograph|survey")),
    # For the optional Legal checklist fill (2026-10-12).
    ("Assignment of the purchase agreement", re.compile(r"assign(ment)?\.? of (agree|psa|purchase|contract)")),
    ("Closing affidavit or certificate", re.compile(r"affidavit|non-?foreign|firpta|survey receipt|"
                                                    r"closing instruction")),
    # For the Proforma fill (2026-10-12): the loan and the recorded obligations.
    ("Loan documents", re.compile(r"promissory|(?<![a-z0-9])note(?![a-z0-9])|deed of trust|loan (agreement|mod)|"
                                  r"modification|refinanc|(?<![a-z0-9])dot(?![a-z0-9])")),
    ("Development or cost-share agreement", re.compile(r"development agreement|dev(elopment)? agmt|cost[ -]share|"
                                                       r"reimbursement agreement|improvement agreement|"
                                                       r"(?<![a-z0-9])da(?![a-z0-9]).*(executed|recorded|rec)")),
    ("Budget or cost estimate", re.compile(r"budget|opinion of probable cost|cost estimate|\bctc\b|"
                                           r"cost to complete|pro ?forma")),
]
# Copies nobody should read: marked unused, superseded or void in their name or
# folder. The pending-deal test was offered two "Not Used" amendments first.
# Term sheets themselves are never a source for a term sheet.
_SET_ASIDE = re.compile(r"not used|unused|superseded|\bvoid\b|do not use|old version|"
                        r"term[ _-]?sheet")
# A signed or recorded copy outranks a draft of the same document.
_EXECUTED = re.compile(r"executed|fully|\bfe\b|signed|recorded|\brec\b|final")
_NEVER = re.compile(r"(?!x)x")
# Lookalikes that are not the thing: a deed of trust is a loan document, a
# cover letter or a receipt page is not the agreement.
_EXCLUDE = {
    "Loan documents": re.compile(r"notes?(?![a-z]) (to|on|from|re)|meeting notes|note ?pad|release|(?<![a-z])rel(?![a-z])|"
                                 r"recon|notice|unsigned|form of"),
    "Deed": re.compile(r"deed of trust|\bdot\b|restriction"),
    "Purchase agreement": re.compile(r"rcpt|receipt|sig(nature)? pg|signature page|assign"),
    # Recorded exception documents sit in a commitment's folder but are not it.
    "Title commitment": re.compile(r"wir(e|ing)|exceptions? /|\bex\b\. ?\d"),
    # The first blind test's first pick was "...Rcpt Cert Good Standing", a
    # packing slip; the certificate itself was filed the day before.
    "Entity status certificate": re.compile(r"rcpt|receipt"),
    # Public-body resolutions are not the owner's: the pending-deal test was
    # offered tax increment and improvement district resolutions.
    "Partnership resolution or consent": re.compile(
        r"tax increment|\btif\b|\bpid\b|public improvement|council|county|city of|ordinance|"
        r"exception|declaration|notice of formation"),
    "County tax records": re.compile(r"tax increment|\btif\b|tax return|k-1|1031|tax credit"),
}
_SUPPORTING_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls"}
_PER_DEAL = 2
_PER_KIND = 4
_DEED_LIMIT = 9
_PSA_PER_DEAL = 2
_PSA_LIMIT = 10
_WALK_SECONDS = 15
_WALK_MAX_FILES = 150_000

# ─── The writing rules ──────────────────────────────────────────────────────
#
# The rules live in the TEAM FOLDER as an ordinary document,
# mini_term_sheets/_reference/Mini Term Sheet Rules.md, read fresh on every
# request (team decision, 2026-10-08): the team expects to keep refining how
# these sheets read, and a rule kept in code would need a published update and
# a restart on every machine for each change. Edited there, it reaches every
# teammate's next sheet through OneDrive. What stays in code is the machinery
# -- finding, reading, writing the workbook -- which rarely changes.
#
# DEFAULT_RULES below is the fallback when that document is missing or cannot
# be read, and the text it was first seeded from. It holds no real facts; the
# repository is public.
#
# How the wording was settled, 2026-10-08: no unexplained abbreviations, no
# semicolons, brackets or slashes, but the professional tone of a real term
# sheet (an over-plain rewrite read as unprofessional), and never drop or
# shorten a fact the source provides. A "no word three times in a cell" rule
# was tried and removed the same day: on legal fields it forced the documents'
# own terms ("recorded", "easement") out of the sheet.
#
# The flow, settled with the team the same day: build from the DD report alone,
# then ASK before opening anything else -- first "may I look for what is
# missing?" (missing fields only), then "shall I confirm the numbers?", where any
# disagreement goes back to the person. Settling everything up front cost tens of
# thousands of tokens and over ten minutes a sheet, which the team called too long.
# The sheet is a short plain summary of the report: six fields explained, seven
# facts only, Seller Contact contact details only, modelled on where the first
# full tool sheet explained and where it simply stated facts.

RULES_FILENAME = "Mini Term Sheet Rules.md"

DEFAULT_RULES = """\
WHAT THE SHEET IS
- A mini term sheet is a short, plain summary of the DD report. Anyone who wants the
  detail reads the report. Write for a partner reading quickly: professional, clear,
  no jargon.
- Spell out abbreviations: "purchase agreement", not PSA; "settlement statement", not
  HUD-1; the appraisal district or assessor by its full name, not its initials; "land
  title survey", not ALTA/NSPS; "formerly known as", not f/k/a; a school district by its
  full name, never ISD; "build-to-rent", not BTR; "Planned Development", not PD.
- Punctuation: full stops and commas. No semicolons, brackets, slashes or dashes between
  words. Names are written exactly as their documents write them.

TWO KINDS OF FIELD
- FACTS ONLY: one short line or sentence. Just the facts, no explanation, no sources.
- EXPLAINED: a short paragraph of two to four sentences. The key facts, plus only the
  explanation a reader needs: where figures disagree, and what must be confirmed. Name
  a source only when two figures disagree.

EACH FIELD
- Property / Project Name (FACTS ONLY): the formal name, then the short name the team
  uses, separated by a comma. Pass the short form, under 40 characters, as the save
  tool's title for the heading.
- Owner (EXPLAINED): the owning entity, any former name, its general partner or manager,
  whether title shows the current name, and what must be confirmed. If the property is
  being acquired, the buyer under contract.
- Seller Contact (CONTACT ONLY): the seller's name, mailing address, telephone and
  email. For a deal being acquired, also each broker's name, firm, telephone and email.
  Nothing else: no history, no sources, no notes. Leave out any detail not given, and
  leave the field empty if no contact detail is given at all.
- Location (FACTS ONLY): the roads or street address, city and ZIP code, the position
  relative to the nearest landmark, and the distance to the nearest major city.
- County (FACTS ONLY): "<Name> County, <State>".
- APN(s) (FACTS ONLY): the appraisal district or assessor once, then the parcel numbers.
- Legal Description (EXPLAINED): the survey and abstract or the recorded plat, the
  pieces excepted from it with acreage and year, and what must be confirmed.
- Parcel Size (Acres) (EXPLAINED): the main acreage figures, with their sources when
  they differ, the usable acreage, and what must happen before an acreage is relied on.
- Lot Size (FACTS ONLY): whether platted lots exist, and the parcels with their acreage.
- Current Zoning (FACTS ONLY): the district, the city, and the rezoning required.
- Proposed Use(s) (EXPLAINED): what the DD report recommends, or the options it
  compares, with the main risk it names. Nothing but the DD report is used here.
- Purchase Price Total (EXPLAINED): if owned, begin "Current transaction price not
  provided." then the historical price and how it was paid. If being acquired, the
  contract price and its basis, any amendment that changes it, and the latest
  settlement figure, saying whether it is signed or estimated.
- Price per Acre (EXPLAINED): if owned, begin "Current transaction price per acre not
  provided." Then the per-acre figure the report prints, with the acreage it rests on.
  If the report prints no per-acre figure, or only a rounded acreage, do NOT calculate
  one: leave the row empty so it is marked for input, and in the questions at the end
  propose the calculation (the price, the acreage you would divide by and where it
  comes from, and the result) for the person to confirm. The team decided this on
  2026-10-12: a per-acre figure is confirmed by a person, never worked out unasked.
- Price per Lot (FACTS ONLY): "Not applicable at the current stage." when no lots exist.

THE OTHER TABS
- Due Diligence: every row is a question. Answer with a short verdict line first ("Not
  completed", "Yes", "Low risk", "Provider not confirmed"), then one or two sentences from
  the report. Put where it came from in the source column ("DD report section 10.2").
- When the report says something has not been done, is not known yet, or depends on
  another step, that IS the answer: write the verdict with the reason ("Not determined.
  Depends on the Phase I findings."). When the report says nothing at all on the
  subject, write only the verdict, as the team's own sheets do: "Not confirmed" (the
  default if the row is left empty), or "Not completed", "Not determined", "Not
  estimated", "Not provided", "Not received", whichever fits. Never NEEDS INPUT on this
  tab, and never "not applicable" for a requirement named after another state.
- Every row is short: one paragraph of two to four sentences, as the team's own sheets
  are. The Proforma's Assumptions and Notable expenses rows may run longer, to about
  the length of a long paragraph, never two. Facts from a document are folded into the
  row's text, not added as a second paragraph; where it came from goes at the end in a
  few words ("Partnership agreement, section 4.4."), never a list of page numbers.
- Legal and Proforma: most rows are steps in a checklist (LOI sent, deed recorded, budget
  approved). Write what the report states, in one line, with its date. A step the report
  does not mention is left empty and is saved as "Not verified", which is not a gap and
  is never asked about. Where the report says the step has not happened, use the team's
  words: "Not drafted", "Not approved", "Not established", "Not provided". The Proforma's Version 2 to 4 blocks stay blank unless the
  report describes a later pro forma.
- Onboarding is a to-do list for the finance team and is never written.
- The Legal checklist is filled from the deal file only when the person says yes to that
  offer. Then use the deal documents' facts in the same short style; write "Not
  provided" where a document the purchase agreement called for is not in the folder,
  "Signed copy on file" where only its file name shows it, and leave "Not verified"
  where nothing was looked for. Write a date only if it was read inside a document
  or is printed in the file's own name; the date a file was saved on the drive is
  not the date it was signed. A row that heads a group ("Survey", "Approval") or
  sums one up ("Summary of dates") gets one line on what was found beneath it. When
  only the signed version of an agreement is on file, its "Drafted" row says so and
  the other drafting steps stay "Not verified". An amendment row with no amendment on
  file says so, and names any addendum signed with the agreement itself.
- The Proforma is filled from the partnership agreement, the newest tax record and the
  loan papers the same way, only on the person's yes: fee rates, the preferred return
  and the profit split from the agreement, property taxes from the tax certificate,
  the loan from the note. The report's own text in a row stays and the document's
  facts follow it. Nothing is modelled: no sale price, return or multiple is worked
  out from these documents.

MISSING AND DISAGREEING
- The sheet is built from the DD report only. A field the report does not give is left
  empty, and is saved as NEEDS INPUT.
- Other documents are opened only after the person agrees, and only to fill the
  missing fields.
- Numbers are checked against the signed documents only when the person asks. Any
  number that disagrees is the person's call: show each figure with its source and a
  recommendation, and change nothing until they answer.
- Quote figures exactly as printed. A figure you calculate is called "approximately".
  Never guess, and never write N/A or TBD."""

DEFAULT_ABBREVIATIONS = ["PSA", "HUD-1", "HUD", "CAD", "ALTA", "NSPS", "f/k/a",
                         "fka", "ISD", "BTR", "PD", "GF No", "N/A", "TBD", "LOI"]
_BAD_PUNCT = re.compile(r";|\(|\)|/| -- | — | – ")


def _rules_document() -> str:
    """The full rules document as first written to the team folder."""
    lines = ["# Mini Term Sheet Rules", "",
             "This document tells Claude how to write every mini term sheet. Edit it here "
             "and every teammate's next sheet follows the change, with no update or restart.",
             "", "Keep the two headings below exactly as they are. Under the first, write "
             "rules in plain sentences. Under the other, one item per line starting with "
             "a dash.", "",
             "## Writing rules", "", DEFAULT_RULES, "",
             "## Abbreviations to spell out", ""]
    lines += [f"- {a}" for a in DEFAULT_ABBREVIATIONS]
    return "\n".join(lines) + "\n"


def _rules_section(doc: str, heading: str) -> str:
    m = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", doc, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def load_rules() -> dict:
    """
    The rules in force: {"text", "abbreviations", "source"}.

    Read from the team folder's rules document on every call, so an edit there
    takes effect on the next sheet. Falls back to the built-in copy, per part,
    when the document or one of its sections is missing -- and says so in
    "source", so a broken document is noticed rather than silently ignored.
    """
    from config import MINI_TERM_SHEETS_REFERENCE_DIR
    out = {"text": DEFAULT_RULES, "abbreviations": DEFAULT_ABBREVIATIONS,
           "source": "built-in copy (no rules document found)"}
    path = MINI_TERM_SHEETS_REFERENCE_DIR / RULES_FILENAME
    try:
        doc = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    items = lambda body: [re.sub(r"^[-*]\s*", "", ln).strip() for ln in body.splitlines()
                          if re.match(r"^\s*[-*]\s+\S", ln)]
    text = _rules_section(doc, "Writing rules")
    missing = []
    if text:
        out["text"] = text
    else:
        missing.append("Writing rules")
    abbr = items(_rules_section(doc, "Abbreviations to spell out"))
    if abbr:
        out["abbreviations"] = abbr
    else:
        missing.append("Abbreviations to spell out")
    out["source"] = f"the team's rules document ({RULES_FILENAME})" + (
        f"; built-in copy used for missing section(s): {', '.join(missing)}" if missing else "")
    return out


def write_default_rules_document() -> Path:
    """Seed the team folder's rules document. Never overwrites an existing one."""
    from config import MINI_TERM_SHEETS_REFERENCE_DIR
    path = MINI_TERM_SHEETS_REFERENCE_DIR / RULES_FILENAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_rules_document(), encoding="utf-8")
    return path


def rules_excerpt(text: str, headings: tuple[str, ...]) -> str:
    """
    Only the named sections of the rules (their headings are the upper-case
    lines). The tab calls repeat the rules four times a sheet; sending the
    whole document each time cost about 6,000 tokens of repetition.
    """
    out, keep = [], False
    for line in text.splitlines():
        if line and line.strip() == line and line.isupper():
            keep = any(line.startswith(h) for h in headings)
        if keep:
            out.append(line)
    return "\n".join(out).strip() or text


# The longest rows on the team's reviewed sheets: about 1,300 characters on the
# Proforma's Assumptions, under 700 everywhere else but the Overview.
ROW_LENGTH_CAP = {"Legal": 700, "Due Diligence": 700, "Proforma": 1400}


def too_long(key: str, text: str) -> bool:
    """A row longer than the team's own sheets ever run (the Overview is not capped)."""
    tab = key.split(":", 1)[0] if ":" in key else "Overview"
    cap = ROW_LENGTH_CAP.get(tab)
    return bool(cap) and len(text or "") > cap


def wording_problems(label: str, text: str, rules: dict | None = None) -> list[str]:
    """Abbreviations and punctuation the rules forbid, as short notes."""
    rules = rules or load_rules()
    found = []
    for term in rules["abbreviations"]:
        if re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text,
                     0 if term.isupper() else re.I):
            found.append(f'"{term}"')
    # Punctuation inside a company name such as "Example & Sons-Test 9, LP"
    # is allowed; brackets, semicolons and slashes never appear in those.
    marks = sorted({m.group(0).strip() or m.group(0) for m in _BAD_PUNCT.finditer(text)})
    found += [f"'{m}'" for m in marks]
    return [f"{label}: {', '.join(found)}"] if found else []


# Written in place of a value Claude could not establish, so the cell is
# impossible to miss and easy to find again when the answer arrives.
NEEDS_INPUT = "NEEDS INPUT"
_GAP_FILL = "FFFFF2CC"


# ─── Finding the report ─────────────────────────────────────────────────────

def _rel(path: Path) -> str:
    from config import CORPUS_DIR
    return path.relative_to(CORPUS_DIR).as_posix()


def _filed_date(name: str) -> str:
    """YYYYMMDD / YYMMDD / YYYY-MM-DD in a file name, as YYYY-MM-DD, or ''."""
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", name)
    if m and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31:
        return f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _find_grouped_property_folders(property_name: str) -> list:
    """
    Property folders one level inside a state's grouping folders, the ones
    named with a leading "!" (!!PENDING, ! SOLD, !PENDING OFFERS - ACTIVE).
    A deal under evaluation lives there -- the pending-deal test property sat at
    <STATE> PROPERTIES/!!PENDING/<Property> -- and the shared finder only
    looks at <STATE>/<Property>, so the first test on a pending deal found
    nothing. Kept here rather than widening the shared finder, which other
    tools use to open folders.
    """
    from config import CORPUS_DIR
    want, out = property_name.lower().strip(), []
    root = CORPUS_DIR / "!PROPERTIES"
    if not want or not root.is_dir():
        return out
    for state in root.iterdir():
        if not state.is_dir():
            continue
        for group in state.iterdir():
            if not (group.is_dir() and group.name.startswith("!")):
                continue
            try:
                out += [p for p in group.iterdir() if p.is_dir() and want in p.name.lower()]
            except OSError:
                continue
    return sorted(out, key=lambda p: p.name)


def property_folder(property_name: str) -> tuple[Path | None, list[Path]]:
    """(the property's folder, every folder that matched). Exact name wins."""
    from mcp_server import _find_property_folders
    matches = _find_property_folders(property_name)
    if not matches:
        matches = _find_grouped_property_folders(property_name)
    if not matches:
        return None, []
    exact = [m for m in matches if m.name.lower() == property_name.lower()]
    return (exact[0] if exact else matches[0] if len(matches) == 1 else None), matches


def walk_names(folder: Path) -> tuple[list[tuple[str, float]], bool]:
    """
    ([(path relative to the property folder, modified time)], finished?).

    Reads the property's folder directly rather than the file list, which is
    rebuilt nightly at best and was weeks old on the day this was built --
    old enough to miss both the final DD report and the title commitment the
    reviewed reference sheet relies on. Names only: listing a folder downloads
    nothing. Measured on the first test property: thousands of files in under a second. Bounded anyway,
    and a walk that runs out of time says so rather than looking complete.
    """
    import os
    import time
    start, out, stack = time.monotonic(), [], [folder]
    while stack:
        if time.monotonic() - start > _WALK_SECONDS or len(out) > _WALK_MAX_FILES:
            return out, False
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.name != "desktop.ini" and not e.name.startswith("~$"):
                        try:
                            mt = e.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            mt = 0.0
                        out.append((Path(e.path).relative_to(folder).as_posix(), mt))
        except OSError:
            continue
    return out, True


def find_dd_reports(folder: Path, names) -> tuple[list[dict], int]:
    """
    Vaulter's own DD reports in a property folder, newest first, and how many
    others (seller / buyer packages, archived copies) were left out.
    """
    rel_folder = _rel(folder)
    out, others = [], 0
    for rel, mt in names:
        name = rel.rsplit("/", 1)[-1]
        if Path(name).suffix.lower() not in _DD_EXT or not _DD_NAME.search(name):
            continue
        if _NOT_OURS.search(rel):
            others += 1
            continue
        out.append({"path": f"{rel_folder}/{rel}", "name": name, "filed": _filed_date(name),
                    "modified": _dt.date.fromtimestamp(mt).isoformat()})
    out.sort(key=lambda r: (r["filed"] or r["modified"], r["modified"]), reverse=True)
    return out, others


def _deal(rel: str) -> str:
    """
    Which deal a file belongs to, from where it is filed: 'Acquisition',
    'Loan / 2026 Refi', 'Disposition / 2026 Sale'. A property folder keeps
    each transaction in its own branch, and a sale of a strip of the land or
    a refinance has its own title commitment and settlement statement -- so
    "the newest title commitment" alone can be the wrong deal's.
    """
    parts = rel.split("/")[:-1]
    if parts and re.match(r"^\d\d\.\s", parts[0]):
        parts = parts[1:]
    if parts and parts[0].lower() == "legal":
        parts = parts[1:]
    label = []
    for p in parts:
        if p.startswith("!") or re.match(r"^\d\d\.\s", p):
            continue                       # "!OFFERS", "!LOANS": grouping folders
        label.append(p)
        if len(label) == 2:
            break
    return " / ".join(label) or "(top of the property folder)"


def find_supporting_documents(folder: Path, names, per_kind: int = _PER_KIND) -> dict[str, list[dict]]:
    """
    Candidate files per kind, by name, grouped by the deal they belong to.

    Matches the file name AND the folder it sits in, because the firm files
    by folder: a settlement statement may be named only "SS ..." inside a
    settlement folder, and a title commitment only "<date> Commit ...". The same file kept under both
    !OFFERS and !RESALES is shown once.
    """
    rel_folder = _rel(folder)
    found: dict[str, list[dict]] = {}
    for kind, pattern in _SUPPORTING:
        by_deal: dict[str, list[dict]] = {}
        seen_names = set()
        for rel, mt in names:
            parts = rel.split("/")
            name = parts[-1]
            if Path(name).suffix.lower() not in _SUPPORTING_EXT:
                continue
            where = " / ".join(parts[-2:-1]) + " / " + name
            if not pattern.search(where.lower()) or _EXCLUDE.get(kind, _NEVER).search(where.lower()):
                continue
            if _SET_ASIDE.search(rel.lower()):
                continue
            if name.lower() in seen_names:
                continue
            seen_names.add(name.lower())
            by_deal.setdefault(_deal(rel), []).append(
                {"path": f"{rel_folder}/{rel}", "name": name, "filed": _filed_date(name),
                 "modified": _dt.date.fromtimestamp(mt).isoformat(),
                 # A file whose own name says what it is beats one that only
                 # sits in a matching folder: "PSA Fully Signed" over the
                 # "Assignment of Agreement" filed beside it.
                 "named": bool(pattern.search(name.lower())),
                 "executed": bool(_EXECUTED.search(name.lower()))})
        # Deeds: one per deal and a longer list, because every piece the title
        # commitment carves out has its own deed, and checking each one's size
        # against the commitment is how the reviewed reference sheet caught a
        # carve-out the title commitment overstated against its own recorded deed.
        is_deed = kind == "Deed"
        # A purchase agreement grows by amendment: the original and each
        # amendment change the price, so a deal under contract needs them all.
        is_psa = kind == "Purchase agreement"
        per_deal = 1 if is_deed else _PSA_PER_DEAL if is_psa else _PER_DEAL
        limit = _DEED_LIMIT if is_deed else _PSA_LIMIT if is_psa else per_kind
        acquisition, rows = [], []
        for deal, docs in by_deal.items():
            docs.sort(key=lambda r: (r["named"], r["executed"], is_deed and "rec" in r["name"].lower(),
                                     r["filed"] or r["modified"], r["modified"]), reverse=True)
            if is_psa:
                # A second copy of the agreement itself ranks behind its amendments:
                # the buyer-signed copy beside the fully signed one crowded out the
                # addendum that set the deposits (2026-10-12).
                amend = lambda r: bool(re.search(r"amend|addend", r["name"], re.I))
                first, later, seen_original = [], [], False
                for r in docs:
                    if not amend(r) and seen_original:
                        later.append(r)
                    else:
                        first.append(r)
                        seen_original = seen_original or not amend(r)
                docs = first + later
            for d in docs[:max(per_deal, _PER_DEAL if deal.lower().startswith("acquisition") else 0)]:
                row = {**d, "deal": deal, "more_in_deal": len(docs) - per_deal}
                # How the firm bought it answers "who sold it, what was paid"
                # for an owned property however many later deals are newer.
                (acquisition if deal.lower().startswith("acquisition") else rows).append(row)
        rows.sort(key=lambda r: (r["filed"] or r["modified"]), reverse=True)
        rows = acquisition[:_PER_DEAL] + rows[:limit - min(len(acquisition), _PER_DEAL)]
        if rows:
            found[kind] = rows
    return found


# A teammate can hand over a report by dropping it here instead of filing it on
# the drive first (team decision, 2026-10-08: "name the file or drop it in a
# folder"). Attaching it to the chat would put the whole report into the
# conversation, tens of thousands of tokens for a typical report.
DD_DROP_NAME = "DD Drop"
DROP_PREFIX = DD_DROP_NAME + "/"


def dd_drop_dir() -> Path:
    from config import MINI_TERM_SHEETS_DIR
    return MINI_TERM_SHEETS_DIR / DD_DROP_NAME


def drop_reports() -> list[dict]:
    """Reports waiting in the DD Drop folder, newest first."""
    try:
        files = [f for f in dd_drop_dir().iterdir()
                 if f.is_file() and f.suffix.lower() in _DD_EXT and not f.name.startswith("~$")]
    except OSError:
        return []
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"path": DROP_PREFIX + f.name, "name": f.name, "filed": _filed_date(f.name),
             "modified": _dt.date.fromtimestamp(f.stat().st_mtime).isoformat()} for f in files]


def resolve_drop(ref: str) -> Path | None:
    """A DD Drop reference ("DD Drop/<name>" or a bare file name in it), or None."""
    name = ref[len(DROP_PREFIX):] if ref.startswith(DROP_PREFIX) else ref
    if "/" in name or "\\" in name or not name.strip():
        return None
    f = dd_drop_dir() / name
    return f if f.is_file() else None


def read_report(ref: str) -> tuple[str, dict]:
    """
    A DD report's text: from the DD Drop folder, else from the library. The drop
    folder is read directly and only for the one file named in it; everything
    else still goes through the library's own scope guard.
    """
    dropped = resolve_drop(ref)
    if dropped is None:
        from corpus import read_document
        return read_document(ref, max_chars=5_000_000)
    from corpus import extract as _x
    meta = {"path": DROP_PREFIX + dropped.name, "filename": dropped.name,
            "file_type": dropped.suffix.lower(), "page_count": 0, "ocr_used": False,
            "comment_count": 0, "truncated": False}
    if dropped.suffix.lower() == ".pdf":
        return _x._extract_pdf(dropped, meta)
    return _x._extract_docx(dropped, meta)


# Which documents can fill which missing field, for "may I look for what is
# missing?". Only these kinds are listed, so a lookup reads only what it needs.
FIELD_SOURCES = {
    "Owner": ["Title commitment", "Entity status certificate", "Partnership resolution or consent"],
    "Seller Contact": ["Purchase agreement", "Settlement statement"],
    "Location": ["Firm investment materials"],
    "County": ["County tax records", "Title commitment"],
    "APN(s)": ["County tax records", "Title commitment"],
    "Legal Description": ["Title commitment", "Deed"],
    "Parcel Size (Acres)": ["Title commitment", "Deed", "County tax records"],
    "Lot Size": ["County tax records", "Title commitment"],
    "Current Zoning": ["Firm investment materials"],
    "Purchase Price Total": ["Purchase agreement", "Settlement statement"],
    "Price per Acre": ["Purchase agreement", "Settlement statement"],
    "Price per Lot": ["Purchase agreement"],
}
# The documents the optional Legal checklist fill reads (team decision,
# 2026-10-12, "Option A"): only on the person's yes, never in the first pass.
LEGAL_SOURCES = ["Letter of intent", "Purchase agreement", "Assignment of the purchase agreement",
                 "Settlement statement", "Deed", "Partnership / operating agreement",
                 "Partnership resolution or consent", "Entity status certificate",
                 "Closing affidavit or certificate"]
_CLOSING_LIST_CAP = 75


def acquisition_file_names(folder: Path, names) -> list[str]:
    """
    Names of the files filed under the property's acquisition, readable or not,
    without opening any. A signed copy on file is evidence a closing step
    happened, and its absence is what "Not provided" means; the closing binder's
    own index was an old-format Word file the reader cannot open.
    """
    out, seen = [], set()
    for rel, _ in names:
        if not _deal(rel).lower().startswith("acquisition"):
            continue
        name = rel.rsplit("/", 1)[-1]
        if Path(name).suffix.lower() == ".msg" or name.lower() in seen or _SET_ASIDE.search(rel.lower()):
            continue
        seen.add(name.lower())
        out.append(rel)
    # Closing papers first, title-exception copies last: on the reference
    # property 37 of 105 acquisition files were title exhibits, and an
    # alphabetical cut dropped closing documents instead of them.
    order = lambda rel: (2 if "/title/" in rel.lower() else 0 if re.search(
        r"closing|binder|purchase agreement|escrow", rel.lower()) else 1, rel)
    out.sort(key=order)
    return out[:_CLOSING_LIST_CAP]


def _file_year(rel: str, mt: float) -> int:
    """The year on the file's own name, else the year it was saved on the drive."""
    filed = _filed_date(rel.rsplit("/", 1)[-1])
    return int(filed[:4]) if filed else _dt.date.fromtimestamp(mt).year


def deal_files(names, deal_year: int, span: int = 1) -> list:
    """
    The files of ONE deal: those dated within a year of the year the DD report
    gives for the purchase (owned) or the contract (being acquired).

    Not a folder branch (2026-10-12): a folder called "Acquisition" is one
    layout, and on two other properties checked the same day the purchase
    papers sat across several sibling folders ("Purchase Agreement", "Escrow
    Documents", "Closing Documents"), while the only branch holding a purchase
    agreement by name was a LATER SALE. A year is a fact from the report; a
    folder name is a guess about how someone filed. The drive date stands in
    where a name carries no date, so a re-synced folder can move a file out of
    its year -- then nothing is found, the years that do hold papers are
    listed, and the person chooses.
    """
    return [(rel, mt) for rel, mt in names
            if abs(_file_year(rel, mt) - deal_year) <= span and not _SET_ASIDE.search(rel.lower())
            and not _SALE_BRANCH.search(_deal(rel))]


# The Legal tab describes the firm's PURCHASE, so the firm's own sales are left
# out: on the pending-deal test a resale to another buyer in the same year
# put that buyer's letter of intent and contract beside the firm's own. The
# firm files its sales under "Disposition" (and "!RESALES"), which is the one
# folder-name rule kept here, because it only ever removes papers, and a sale
# paper that slips through is still labelled with its branch in the reply.
_SALE_BRANCH = re.compile(r"^(disposition|resales?|!resales|sale)(?![a-z])", re.I)


def deal_years(folder: Path, names) -> list[tuple[int, int]]:
    """(year, how many purchase agreements, settlement statements or deeds carry it)."""
    found = find_supporting_documents(folder, [(r, m) for r, m in names
                                               if not _SALE_BRANCH.search(_deal(r))])
    count: dict[int, int] = {}
    for kind in ("Purchase agreement", "Settlement statement", "Deed"):
        for r in found.get(kind, []):
            y = int((r["filed"] or r["modified"])[:4])
            count[y] = count.get(y, 0) + 1
    return sorted(count.items())


# What the server reads for the Legal fill, in order, and how many of each.
# Eight documents at most, as the person-driven version allowed.
_LEGAL_READ = [("Letter of intent", 1), ("Purchase agreement", 4), ("Assignment of the purchase agreement", 1),
               ("Settlement statement", 1), ("Deed", 1), ("Partnership / operating agreement", 1),
               ("Partnership resolution or consent", 1), ("Closing affidavit or certificate", 2)]
_LEGAL_DOC_CAP = 8
_LEGAL_PASSAGE_CAP = 650
_LEGAL_DOC_CAP_CHARS = 6500     # per document, so the last papers read are not the ones cut
_LEGAL_TEXT_CAP = 32000
LEGAL_READ_BUDGET_SECONDS = 90


def legal_documents(folder: Path, names, deal_year: int) -> list[dict]:
    """The deal's own papers to read, best copy of each kind first."""
    found = find_supporting_documents(folder, deal_files(names, deal_year))
    out = []
    for kind, n in _LEGAL_READ:
        rows = found.get(kind, [])
        if kind == "Purchase agreement":
            # The agreement itself first, then its amendments newest first: on
            # the pending-deal test the three slots went to two copies of the
            # third amendment and the second, and the agreement was never read.
            amend = lambda r: bool(re.search(r"amend|addend", r["name"], re.I))
            rows = [r for r in rows if not amend(r)] + sorted([r for r in rows if amend(r)],
                                                            key=lambda r: r["filed"] or r["modified"], reverse=True)
        # One copy of each paper: a signing-service copy beside the executed
        # one (same kind, same filed date), or a second copy of the agreement
        # itself, adds reading time and says nothing new.
        kept, seen = [], set()
        for r in rows:
            stamp = ("original" if kind == "Purchase agreement"
                     and not re.search(r"amend|addend", r["name"], re.I) else r["filed"] or r["name"])
            if stamp in seen:
                continue
            seen.add(stamp)
            kept.append({**r, "kind": kind})
        out += kept[:n]
    return out[:_LEGAL_DOC_CAP]


# The questions put to each kind of paper, the ones that filled 33 of the
# reference sheet's 39 Legal rows when a reader asked them by hand (2026-10-12).
# Asked here instead, so the reader gets the answers in one reply.
_LEGAL_QUESTIONS = {
    "Letter of intent": ["letter of intent purchase price terms deposit dated signed accepted counter"],
    "Purchase agreement": [
        "seller buyer parties effective date purchase price sales price contract",
        "earnest money deposit additional deposit extension deposit amount due date paid escrow agent",
        "feasibility period inspection period approval notice days after effective date",
        "seller deliveries materials survey title commitment objections cure period closing date documents",
    ],
    "Assignment of the purchase agreement": [
        "assignor assigns assignee agreement escrow earnest money dated signed by"],
    "Settlement statement": [
        "settlement date sales price earnest money deposit credit cash from borrower seller signed"],
    "Deed": ["grantor grantee special warranty deed vendor's lien note dated recorded volume page instrument"],
    "Partnership / operating agreement": [
        "partnership agreement effective date general partner limited partners executed signed by"],
    "Partnership resolution or consent": ["resolved authorized to sign purchase closing documents consent dated"],
    "Closing affidavit or certificate": ["affidavit certification signed by dated sworn"],
}
_LEGAL_PER_QUESTION = 3


def _row_subjects(r: dict) -> set:
    low = r["label"].lower()
    where = (r["section"] or "").lower()
    hint = " ".join(h for k, h in _CHECKLIST_HINTS.items() if k in low or k in where)
    return set(re.findall(r"[a-z]{4,}", low + " " + hint)) - _GATE_SKIP


def legal_evidence(folder: Path, names, deal_year: int, rows: list[dict], current: dict | None = None,
                   budget: float = LEGAL_READ_BUDGET_SECONDS) -> tuple[str, dict]:
    """The Legal tab: the deal's own papers, chosen by the report's year."""
    mine = [rel.rsplit("/", 1)[-1] for rel, _ in deal_files(names, deal_year)
            if not rel.lower().endswith(".msg")]
    # A re-synced drive date put 2017 exhibits in a 2026 deal; a name that
    # prints a year of its own outside the window is not this deal's.
    mine = [n for n in mine if not any(abs(int(y) - deal_year) > 1
                                       for y in re.findall(r"(?<!\d)(19\d\d|20\d\d)(?!\d)", n))]
    return document_evidence(legal_documents(folder, names, deal_year), _LEGAL_QUESTIONS, rows,
                             current, mine, budget)


# What the Proforma fill reads: the partnership agreement and its amendments
# (fees, preferred return, profit split), the newest tax record, the loan
# papers, any recorded cost-share agreement and any budget. Not tied to the
# deal's year: the tax certificate and the loan are the NEWEST ones.
_PROFORMA_READ = [("Partnership / operating agreement", 2), ("County tax records", 1),
                  ("Loan documents", 2), ("Development or cost-share agreement", 1),
                  ("Budget or cost estimate", 1)]
_PROFORMA_QUESTIONS = {
    "Partnership / operating agreement": [
        "acquisition fee disposition fee percent of gross purchase price sales price payable to general partner",
        "management fee annual amount per year payable quarterly in advance",
        "priority return preferred return percent cumulative unreturned capital contributions",
        "distributions order return of capital contributions priority return then percentage interests",
        "percentage interests schedule exhibit partners percent names capital contributions",
    ],
    "County tax records": ["tax certificate taxes due paid balance base tax total year certificate date",
                           "assessed value account acres land value estimated taxes tax rate exemption"],
    "Loan documents": ["principal amount interest rate maturity date payments lender borrower note dated",
                       "modification extended maturity interest rate changed amendment"],
    "Development or cost-share agreement": [
        "cost share road costs responsible for amount payments schedule reimbursement lien release"],
    "Budget or cost estimate": ["total budget entitlement cost estimate per lot per acre approved dated"],
}


def proforma_documents(folder: Path, names) -> list[dict]:
    """The newest of each kind the Proforma fill reads, the signed agreement first."""
    # Obligations are read wherever they are filed: a cost share often comes
    # from a sale, so sales are not left out here as they are for the Legal tab.
    # A longer list per kind than the lookups use: with four, the acquisition
    # branch's two copies and the two newest amendments crowded out the
    # partnership agreement itself.
    found = find_supporting_documents(folder, names, per_kind=12)
    out = []
    for kind, n in _PROFORMA_READ:
        rows = found.get(kind, [])
        if kind == "Partnership / operating agreement":
            # The partnership's own agreement (fees, preferred return, split)
            # before the general partner's operating agreement, which the
            # first pick took instead; then the amendments, newest first.
            amend = lambda r: bool(re.search(r"amend", r["name"], re.I))
            lp = lambda r: bool(re.search(r"partnership|lllp|(?<![a-z])lp(?![a-z])|limited", r["name"], re.I))
            orig = sorted([r for r in rows if not amend(r)], key=lambda r: (lp(r), r["executed"]), reverse=True)
            am = [r for r in rows if amend(r)]
            am = [r for r in am if r["executed"]] or am
            # The agreement and its newest signed amendment: the general
            # partner's operating agreement and an older amendment cost the
            # first blind run its time budget (7 of 8 read in 91s) and said
            # nothing the two did not.
            rows = orig[:1] + sorted(am, key=lambda r: r["filed"] or r["modified"], reverse=True)[:1]
        elif kind == "Loan documents":
            # The note or its modification states the terms; a deed of trust only secures it.
            note = lambda r: bool(re.search(r"note|promissory|modif|amend|loan agreement", r["name"], re.I))
            rows = sorted(rows, key=lambda r: (note(r), r["filed"] or r["modified"], r["executed"]), reverse=True)
        elif kind == "Budget or cost estimate":
            rows = sorted(rows, key=lambda r: r["filed"] or r["modified"], reverse=True)
        else:
            rows = sorted(rows, key=lambda r: (r["executed"], r["filed"] or r["modified"]), reverse=True)
        seen, kept = set(), []
        for r in rows:
            # One copy per filed date: the firm keeps a signed and a
            # countersigned copy of the same amendment side by side.
            stamp = (r["filed"] or r["name"].lower())
            if stamp in seen:
                continue
            seen.add(stamp)
            kept.append({**r, "kind": kind})
        out += kept[:n]
    return out[:_LEGAL_DOC_CAP]


def proforma_evidence(folder: Path, names, rows: list[dict], current: dict | None = None,
                      budget: float = LEGAL_READ_BUDGET_SECONDS) -> tuple[str, dict]:
    """The Proforma tab: the partnership, tax, loan and cost-share papers."""
    mine = [rel.rsplit("/", 1)[-1] for rel, _ in names
            if not rel.lower().endswith(".msg") and not _SET_ASIDE.search(rel.lower())
            and not _SALE_BRANCH.search(_deal(rel))]
    return document_evidence(proforma_documents(folder, names), _PROFORMA_QUESTIONS, rows,
                             current, mine, budget)


def document_evidence(docs: list[dict], questions: dict, rows: list[dict], current: dict | None,
                      mine: list[str], budget: float) -> tuple[str, dict]:
    """
    The deal's own papers, read here and asked the questions a reader would
    ask, so one reply carries the passages and the reader fills the rows. The
    first Option A test cost nine tool calls and 69,000 characters for one tab;
    the documents are read on this computer either way, so the asking might
    as well happen here and only the matching passages travel.

    Reading stops between documents when the budget is spent (a scanned 2007
    purchase agreement can take a minute the first time), names what was not
    read, and a second call finishes it: scanned pages are remembered.
    """
    import time
    from corpus import read_document
    from corpus.find import find

    parts, read, unread, skipped, t0 = [], [], [], [], time.monotonic()
    for d in docs:
        if time.monotonic() - t0 > budget:
            unread.append(d["name"])
            continue
        try:
            text, meta = read_document(d["path"], max_chars=5_000_000)
        except Exception as e:
            text = f"[Could not read: {e}]"
        if text.startswith(("[Cannot read", "[Could not read")):
            skipped.append(f"{d['name']} ({text.strip('[]')[:80]})")
            continue
        read.append(d)
        scanned = "- OCR]" in text
        parts.append(f"== {d['kind'].upper()}: {d['name']} [{d['deal']}]"
                     + (" -- a scan, words and figures can be garbled" if scanned else "") + " ==")
        used, doc_chars = set(), 0
        for q in questions.get(d["kind"], []):
            if doc_chars > _LEGAL_DOC_CAP_CHARS:
                parts.append("  [more passages from this document cut to keep the reply short]")
                break
            found, _ = find(text, q, _LEGAL_PER_QUESTION + 2)
            kept = []
            for f in found:
                key = f["text"][:200]
                if key in used:
                    continue
                used.add(key)
                kept.append(f"  ({f['heading']}) " + f["text"][:_LEGAL_PASSAGE_CAP].replace("\n", "\n  "))
                if len(kept) == _LEGAL_PER_QUESTION:
                    break
            if kept:
                block = f"asked: {q}\n" + "\n".join(kept)
                doc_chars += len(block)
                parts.append(block)
    text_out = "\n".join(parts)
    if len(text_out) > _LEGAL_TEXT_CAP:
        text_out = text_out[:_LEGAL_TEXT_CAP] + "\n[... cut here to keep this short ...]"

    # The rows, what each says now, and the deal's files whose names match a
    # row -- a signed copy on file is evidence a step happened, and its absence
    # is what "Not provided" means.
    current = current or {}
    keys = [r["key"] for r in rows]
    heads = {k for k in keys if any(o.startswith(k + " > ") for o in keys)
             or k.rsplit(" > ", 1)[-1] == "Legal"}
    row_lines, section, any_now = [], None, False
    for r in rows:
        if r["section"] != section:
            section = r["section"]
            row_lines.append(f"[{section}]")
        line = f"  {r['key']}" + ("\n    (heads the rows below it)" if r["key"] in heads else "")
        cur = current.get(r["key"], "")
        if cur and cur != NOT_VERIFIED:
            line += f"\n    now: {cur}"
            any_now = True
        # A name counts only if it carries a word of the row's OWN label, not
        # just the helper words: on the pending-deal test two words from a
        # broad hint matched 2017 bill-of-sale exhibits to the purchase
        # agreement rows and 2006 encroachment agreements to the legal
        # description, while the papers actually read matched nothing.
        label_words = set(re.findall(r"[a-z]{4,}", r["label"].lower())) - _GATE_SKIP
        # "Assignment of School Agreement" must match on "school", not on
        # "assignment" or "agreement", which every deal paper carries: an
        # infrastructure agreement's assignment was offered for four rows.
        label_words = (label_words - {"agreement", "assignment", "certification", "letter"}) or label_words
        words = _row_subjects(r)
        on_file = sorted({n for n in mine if len([w for w in words if w in n.lower()]) >= 2
                          and any(w in n.lower() for w in label_words)})[:4]
        if on_file:
            line += "\n    on file by name: " + "; ".join(on_file)
        row_lines.append(line)
    head = ("ROWS (exact key on its own line; now: what the sheet says; on file by name: the deal's "
            "files that match)" + ("" if any_now else "\n(no Legal row holds text yet, so there is "
                                                   "nothing to keep)"))
    return text_out + "\n\n" + head + "\n" + "\n".join(row_lines), {"read": [d["name"] for d in read], "unread": unread,
                                   "seconds": round(time.monotonic() - t0, 1), "files_in_deal": len(mine), "skipped": skipped}


# The documents a numbers check compares against.
NUMBER_SOURCES = ["Purchase agreement", "Settlement statement", "Title commitment", "Deed",
                  "County tax records"]

# The numbers check across every tab (owner, 2026-10-12): the sheet's own
# figures, wherever they sit, against the signed and recorded papers. The
# acquisition's agreement, settlement statement and deed (the year the report
# gives), the newest title commitment and tax record.
_NUMBERS_READ = [("Purchase agreement", 2), ("Settlement statement", 1), ("Deed", 2),
                 ("Title commitment", 1), ("County tax records", 1)]
_NUMBERS_QUESTIONS = {
    "Purchase agreement": ["purchase price sales price per acre acres total consideration",
                           "earnest money deposit amount additional deposit extension"],
    "Settlement statement": ["sales price contract price deposits credited cash due from borrower seller"],
    "Deed": ["acres tract legal description consideration note amount recorded volume page instrument"],
    "Title commitment": ["acres legal description parcel exceptions amount of insurance effective date"],
    "County tax records": ["account parcel number acres assessed value taxes due year"],
}
_FIGURE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:million|m\b))?|"
                     r"\d[\d,]*(?:\.\d+)?\s*(?:gross |net |usable |total )?acres?|"
                     r"(?<![\w.])\d{2,3}[-.]\d{2,3}[-.]\d{2,4}(?:[-.]\d{1,4})*(?![\w])", re.I)


def numbers_documents(folder: Path, names, deal_year: int | None) -> list[dict]:
    """The deal's own papers for the year given, plus the newest title and tax record."""
    found_deal = find_supporting_documents(folder, deal_files(names, deal_year) if deal_year else names)
    found_all = find_supporting_documents(folder, names, per_kind=12)
    out = []
    for kind, n in _NUMBERS_READ:
        if kind in ("Title commitment", "County tax records"):
            rows = sorted(found_all.get(kind, []), key=lambda r: r["filed"] or r["modified"], reverse=True)
        else:
            rows = found_deal.get(kind, [])
            if kind == "Purchase agreement":
                amend = lambda r: bool(re.search(r"amend|addend", r["name"], re.I))
                rows = [r for r in rows if not amend(r)][:1] + sorted(
                    [r for r in rows if amend(r)], key=lambda r: r["filed"] or r["modified"], reverse=True)
        seen, kept = set(), []
        for r in rows:
            stamp = r["filed"] or r["name"].lower()
            if stamp in seen:
                continue
            seen.add(stamp)
            kept.append({**r, "kind": kind})
        out += kept[:n]
    return out[:_LEGAL_DOC_CAP]


def sheet_figures(property_name: str, existing: str) -> list[tuple[str, str, str]]:
    """(tab, row key, the figures in it) for every row on every tab that holds a figure."""
    out = []
    for tab in TABS:
        if tab == "Onboarding":
            continue
        for key, val in current_values(property_name, existing, tab):
            figs = [m.group(0).strip().rstrip(",.") for m in _FIGURE.finditer(val or "")]
            if figs and not (val or "").upper().startswith(NEEDS_INPUT):
                out.append((tab, key, "; ".join(dict.fromkeys(figs))))
    return out


def numbers_evidence(folder: Path, names, deal_year: int | None, budget: float = LEGAL_READ_BUDGET_SECONDS
                     ) -> tuple[str, dict]:
    """The papers' own figures, read here, for the check to compare against."""
    return document_evidence(numbers_documents(folder, names, deal_year), _NUMBERS_QUESTIONS, [], {}, [], budget)


def documents_for(folder: Path, names, kinds: list[str]) -> dict[str, list[dict]]:
    """The supporting documents of the given kinds only."""
    found = find_supporting_documents(folder, names)
    return {k: found[k] for k in kinds if k in found}


# ─── Reading only what the Overview needs ───────────────────────────────────

def _clean(text: str) -> str:
    """Word-to-text leftovers that cost tokens and carry nothing."""
    text = re.sub(r'<a id="[^"]*"></a>', "", text)
    text = re.sub(r"\[\\\[(\d+)\\\]\]\(#[^)]*\)", r"[\1]", text)    # footnote refs
    text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", r"\1", text)    # markdown escapes
    text = text.replace("__", "")
    return text


def _section(text: str, heading: re.Pattern, cap: int) -> str:
    """
    The first section whose heading matches and which has a real body. The
    body test matters: a report's table of contents repeats every heading
    with nothing under it, and the first test sent that instead of the
    executive summary.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        h = re.match(r"^#{1,4}\s+(.*)$", line)
        if h and heading.search(h.group(1)):
            body = []
            for nxt in lines[i + 1:]:
                if re.match(r"^#{1,4}\s+", nxt):
                    break
                if nxt.strip():
                    body.append(nxt.strip())
            block = "\n".join(body)
            if len(block) > 200:
                return f"[{h.group(1).strip()}]\n" + block[:cap]
    return ""


def _fact_section(text: str) -> str:
    """The report's own fact table, if it has a heading saying so."""
    for heading in _FACT_SECTIONS:
        found = _section(text, heading, _FACT_SECTION_CAP)
        if found:
            return found
    return ""


def gather_evidence(rel_path: str) -> tuple[str, dict]:
    """
    (the passages to send, what was read). Read on this machine; nothing is
    sent anywhere but back to the conversation that asked.
    """
    from corpus.find import find
    text, meta = read_report(rel_path)
    if text.startswith(("[Cannot read", "[Could not read")):
        return text, meta
    text = _clean(text)
    meta["clean_chars"] = len(text)

    parts, used = [], set()
    facts = _fact_section(text)
    if facts:
        parts.append("== The report's own fact table ==\n" + facts)
        used.add(facts[:200])

    # The executive summary names the project and frames the deal.
    opening = _section(text, _OPENING, _OPENING_CAP)
    if not opening:
        opening = re.sub(r"\n{2,}", "\n", text[:_OPENING_CAP])
    parts.append("== Opening of the report ==\n" + opening)
    used.add(opening[:200])

    for field, query in _FIELD_QUERIES.items():
        found, _ = find(text, query, 8)
        keep = []
        for p in found:
            key = p["text"][:200]
            if key in used or (facts and p["text"][:120] in facts):
                continue
            used.add(key)
            keep.append(f"({p['heading']})\n{p['text']}")
            if len(keep) == _PASSAGES_PER_FIELD:
                break
        if keep:
            parts.append(f"== Found while looking for {field} ==\n" + "\n\n".join(keep))

    # Each passage is sent once, under the FIRST field that found it, so the
    # purchase price can arrive under "Owner". Sending it twice costs tokens;
    # saying so costs one line. (First blind test, 2026-10-08.)
    out = ("Each passage appears once, under the first field it was found for. Read ALL "
           "of them for EVERY field -- a fact you need may sit under another heading.\n\n"
           + "\n\n".join(parts))
    if len(out) > _TOTAL_CAP:
        out = out[:_TOTAL_CAP] + "\n[... passages cut here to keep this short ...]"
    meta["sent_chars"] = len(out)
    return out, meta


# ─── The reviewed example and the template ─────────────────────────────────

# The one workbook every sheet is copied from: logo, layout, labels. A fixed
# name, kept apart from the wording example (team request, 2026-10-08: "can't
# we just copy and paste so it doesn't keep changing"). Until then the newest
# .xlsx in _reference served as both, so a blind test that swapped in another
# property's sheet as the example also moved the Vaulter logo, and anyone
# dropping a sheet into _reference would have changed every future sheet's look.
TEMPLATE_FILENAME = "Mini Term Sheet Template.xlsx"


def reference_workbook() -> Path | None:
    """The reviewed WORDING example in _reference/ (newest .xlsx, never the template)."""
    from config import MINI_TERM_SHEETS_REFERENCE_DIR
    try:
        books = [p for p in MINI_TERM_SHEETS_REFERENCE_DIR.glob("*.xlsx")
                 if not p.name.startswith("~$") and p.name != TEMPLATE_FILENAME]
    except OSError:
        return None
    return max(books, key=lambda p: p.stat().st_mtime) if books else None


def template_workbook() -> Path | None:
    """The LAYOUT every sheet is copied from: the fixed template, else the example."""
    from config import MINI_TERM_SHEETS_REFERENCE_DIR
    fixed = MINI_TERM_SHEETS_REFERENCE_DIR / TEMPLATE_FILENAME
    return fixed if fixed.is_file() else reference_workbook()


def style_examples() -> list[tuple[str, str]]:
    """(label, the reviewed wording) for each Overview row of the example."""
    import openpyxl
    ref = reference_workbook()
    if not ref:
        return []
    wb = openpyxl.load_workbook(ref, read_only=True)
    try:
        ws = wb["Overview"]
        rows = []
        for a, b in ws.iter_rows(min_row=5, max_row=40, min_col=1, max_col=2, values_only=True):
            if a and str(a).strip() in OVERVIEW_FIELDS:
                rows.append((str(a).strip(), str(b or "").strip()))
        return rows
    finally:
        wb.close()      # a read-only workbook keeps the file locked on Windows




# ─── Writing the workbook ───────────────────────────────────────────────────
#
# The workbook is edited as the file Excel wrote, not rebuilt. The first
# version used openpyxl, which re-creates every drawing when it saves: on the
# first test the Vaulter logo came back moved, enlarged, uncropped and with
# an outline added. The team asked for the sign and layout to be exactly the
# original's, so every part of the reviewed workbook is now copied byte for
# byte and only the answer cells change. Anything this code does not know
# about -- the logo, print settings, fonts, borders -- cannot be damaged,
# because it is never touched.

_CELL = re.compile(r'<c r="([A-Z]+)(\d+)"([^>]*?)(?:/>|>(.*?)</c>)', re.S)
_ROW_CELL = re.compile(r'<c r="[A-Z]+\d+"[^>]*?(?:/>|>.*?</c>)', re.S)
_SI = re.compile(r"<si>(.*?)</si>", re.S)
_T = re.compile(r"<t(?: [^>]*)?>(.*?)</t>", re.S)


def _xml_text(fragment: str) -> str:
    import html
    return html.unescape("".join(_T.findall(fragment)))


def _xml_escape(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(attrs: str, name: str) -> str:
    m = re.search(rf'(?<![\w:]){re.escape(name)}="([^"]*)"', attrs)
    return m.group(1) if m else ""


def _col_key(cell_xml: str):
    col = re.match(r'<c r="([A-Z]+)', cell_xml).group(1)
    return (len(col), col)


class _Book:
    """One .xlsx held as its parts, with just enough reading to edit cells."""

    def __init__(self, path: Path):
        import zipfile
        with zipfile.ZipFile(path) as z:
            self.order = z.namelist()
            self.parts = {n: z.read(n) for n in self.order}
        self.sst = [m.group(1) for m in _SI.finditer(self.text("xl/sharedStrings.xml"))]
        rels = self.text("xl/_rels/workbook.xml.rels")
        targets = {}
        for m in re.finditer(r"<Relationship [^>]*?/?>", rels):
            targets[_attr(m.group(0), "Id")] = _attr(m.group(0), "Target")
        self.sheets = {}
        for m in re.finditer(r"<sheet [^>]*?/>", self.text("xl/workbook.xml")):
            name, target = _attr(m.group(0), "name"), targets.get(_attr(m.group(0), "r:id"), "")
            if name and target:
                self.sheets[name] = "xl/" + target.lstrip("/").removeprefix("xl/")

    def text(self, part: str) -> str:
        return self.parts.get(part, b"").decode("utf-8")

    def put(self, part: str, text: str) -> None:
        self.parts[part] = text.encode("utf-8")

    def cell_text(self, attrs: str, inner) -> str:
        inner = inner or ""
        kind = _attr(attrs, "t")
        if kind == "s":
            v = re.search(r"<v>(\d+)</v>", inner)
            return _xml_text(self.sst[int(v.group(1))]) if v else ""
        if kind == "inlineStr":
            return _xml_text(inner)
        v = re.search(r"<v>(.*?)</v>", inner, re.S)
        return v.group(1) if v else ""

    def cells(self, sheet: str):
        """(column, row, attrs, inner) for every cell in a sheet."""
        for m in _CELL.finditer(self.text(self.sheets[sheet])):
            yield m.group(1), int(m.group(2)), m.group(3), m.group(4)

    def set_cell(self, sheet: str, ref: str, text, style=None) -> None:
        """Write text into a cell (None empties it), keeping its style unless given one."""
        xml = self.text(self.sheets[sheet])
        m = re.search(rf'<c r="{ref}"([^>]*?)(?:/>|>(.*?)</c>)', xml, re.S)
        s = style if style is not None else (_attr(m.group(1), "s") if m else "")
        s_attr = f' s="{s}"' if s else ""
        new = (f'<c r="{ref}"{s_attr}/>' if text is None else
               f'<c r="{ref}"{s_attr} t="inlineStr"><is><t xml:space="preserve">'
               f'{_xml_escape(text)}</t></is></c>')
        if m:
            xml = xml[:m.start()] + new + xml[m.end():]
        else:
            row = re.search(r"\d+", ref).group(0)
            rm = re.search(rf'<row r="{row}"[^>]*?(?:/>|>.*?</row>)', xml, re.S)
            if not rm:
                raise ValueError(f"row {row} is not in the template")
            open_tag = re.match(r"<row [^>]*?/?>", rm.group(0)).group(0)
            cells = _ROW_CELL.findall(rm.group(0)) + [new]
            # Cells must stay in column order, or Excel calls the file damaged.
            cells.sort(key=_col_key)
            r = open_tag.rstrip("/>").rstrip("/") + ">" + "".join(cells) + "</row>"
            xml = xml[:rm.start()] + r + xml[rm.end():]
        self.put(self.sheets[sheet], xml)

    def set_row_height(self, sheet: str, row: int, height: float) -> None:
        def fix(m):
            tag = re.sub(r'\s(ht|customHeight)="[^"]*"', "", m.group(0))
            return tag.replace(f'<row r="{row}"', f'<row r="{row}" ht="{height}" customHeight="1"', 1)
        self.put(self.sheets[sheet],
                 re.sub(rf'<row r="{row}"[^>]*>', fix, self.text(self.sheets[sheet]), count=1))

    # One extra cell style marks a field still needing input: the template's
    # own answer style with a pale yellow fill.
    def _xfs(self):
        styles = self.text("xl/styles.xml")
        block = re.search(r"<cellXfs\b[^>]*>(.*?)</cellXfs>", styles, re.S)
        return styles, block, re.findall(r"<xf\b[^>]*?(?:/>|>.*?</xf>)", block.group(1), re.S)

    def _gap_fill_id(self):
        fills = re.search(r"<fills\b[^>]*>(.*?)</fills>", self.text("xl/styles.xml"), re.S)
        for i, f in enumerate(re.findall(r"<fill>.*?</fill>|<fill/>", fills.group(1), re.S)):
            if _GAP_FILL in f:
                return i
        return None

    @staticmethod
    def _without_fill(xf: str) -> str:
        return re.sub(r'\s(fillId|applyFill)="[^"]*"', "", xf)

    def gap_style(self, base: str) -> str:
        fid = self._gap_fill_id()
        if fid is None:
            styles = self.text("xl/styles.xml")
            fm = re.search(r"<fills\b[^>]*>(.*?)</fills>", styles, re.S)
            fid = len(re.findall(r"<fill>.*?</fill>|<fill/>", fm.group(1), re.S))
            new_fill = (f'<fill><patternFill patternType="solid"><fgColor rgb="{_GAP_FILL}"/>'
                        f'<bgColor indexed="64"/></patternFill></fill>')
            opening = re.sub(r'count="\d+"', f'count="{fid + 1}"',
                             re.match(r"<fills\b[^>]*>", fm.group(0)).group(0))
            self.put("xl/styles.xml", styles[:fm.start()] + opening + fm.group(1)
                     + new_fill + "</fills>" + styles[fm.end():])
        styles, block, xfs = self._xfs()
        want = self._without_fill(xfs[int(base or 0)]).replace(
            "<xf ", f'<xf fillId="{fid}" applyFill="1" ', 1)
        if want in xfs:
            return str(xfs.index(want))
        opening = re.sub(r'count="\d+"', f'count="{len(xfs) + 1}"',
                         re.match(r"<cellXfs\b[^>]*>", block.group(0)).group(0))
        self.put("xl/styles.xml", styles[:block.start()] + opening + block.group(1)
                 + want + "</cellXfs>" + styles[block.end():])
        return str(len(xfs))

    def plain_style(self, style: str) -> str:
        """The style a cell had before it was marked as needing input."""
        fid = self._gap_fill_id()
        _, _, xfs = self._xfs()
        xf = xfs[int(style or 0)]
        if fid is None or f'fillId="{fid}"' not in xf:
            return style
        for i, other in enumerate(xfs):
            if self._without_fill(other) == self._without_fill(xf) and f'fillId="{fid}"' not in other:
                return str(i)
        return style

    def save(self, path: Path) -> None:
        """Keep only the shared strings still used, then write the file."""
        import zipfile
        used: dict = {}
        pat = re.compile(r'(<c r="[A-Z]+\d+"[^>]*?\bt="s"[^>]*>\s*<v>)(\d+)(</v>)')
        for sheet in self.sheets.values():
            for m in pat.finditer(self.text(sheet)):
                used.setdefault(int(m.group(2)), len(used))
        for sheet in self.sheets.values():
            self.put(sheet, pat.sub(lambda m: f"{m.group(1)}{used[int(m.group(2))]}{m.group(3)}",
                                    self.text(sheet)))
        if "xl/sharedStrings.xml" in self.parts:
            # Not tidiness: a cleared answer stays in the file's string list,
            # hidden but readable, and would travel into every other
            # property's sheet.
            old = self.text("xl/sharedStrings.xml")
            opening = re.search(r"<sst\b[^>]*>", old)
            tag = re.sub(r'\s(count|uniqueCount)="\d+"', "", opening.group(0))
            tag = tag[:-1] + f' count="{len(used)}" uniqueCount="{len(used)}">'
            items = [""] * len(used)
            for old_i, new_i in used.items():
                items[new_i] = f"<si>{self.sst[old_i]}</si>"
            self.put("xl/sharedStrings.xml", old[:opening.start()] + tag + "".join(items) + "</sst>")
        tmp = path.with_name("~tmp_" + path.name)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in self.order:
                z.writestr(n, self.parts[n])
        try:
            tmp.replace(path)
        except PermissionError:
            # Excel holds the file open; Windows refuses the swap. The half
            # file is removed and the cause named, because the blind Proforma
            # test (2026-10-12) got a raw "Access is denied" and could not tell
            # whether to retry, and the temporary file was left in the folder.
            tmp.unlink(missing_ok=True)
            raise SheetOpenError(f"{path.name} is open in Excel (or read-only), so it cannot be "
                                 "saved. Ask the user to close it, then save again with the "
                                 "same fields.") from None


class SheetOpenError(OSError):
    """The workbook is open in Excel; the save was refused and nothing was written."""


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9&]+", "_", name).strip("_") or "Property"


def property_sheet_folder(property_name: str) -> Path:
    """
    The property's own folder inside mini_term_sheets (team request,
    2026-10-08: one folder per property, so several sheets for one property
    are found together).

    Named after the property's folder on the drive when exactly one matches,
    so a short name, a longer one and the formal name all land in the same
    place. A deal with no folder on the drive yet uses the name as given,
    cleaned of characters Windows refuses in a folder name.
    """
    from config import MINI_TERM_SHEETS_DIR
    name = ""
    try:
        folder, _ = property_folder(property_name)
        name = folder.name if folder else ""
    except Exception:
        pass
    if not name:
        name = re.sub(r"\s+", " ", re.sub(r'[<>:"/\\|?*]+', " ", property_name)).strip(" .") or "Property"
    if name.lower() == MINI_TERM_SHEETS_REFERENCE_NAME:
        name += " property"
    return MINI_TERM_SHEETS_DIR / name


MINI_TERM_SHEETS_REFERENCE_NAME = "_reference"


def _find_existing_sheet(property_name: str, filename: str) -> Path:
    """A sheet made earlier: in its property's folder, else anywhere but _reference."""
    from config import MINI_TERM_SHEETS_DIR
    name = Path(filename).name
    first = property_sheet_folder(property_name) / name
    if first.is_file():
        return first
    found = [p for p in MINI_TERM_SHEETS_DIR.rglob(name)
             if p.is_file() and MINI_TERM_SHEETS_REFERENCE_NAME not in p.parts]
    if len(found) == 1:
        return found[0]
    if found:
        raise FileNotFoundError(f"{name} is in more than one property folder: "
                                + ", ".join(p.parent.name for p in found))
    raise FileNotFoundError(f"{name} is not in the mini term sheets folder")


def _is_blank_line(text: str) -> bool:
    """'________________' is the template's own blank, not an answer."""
    return bool(text.strip()) and set(text.strip()) <= {"_"}


# ─── Every row of every tab, named from the template ───────────────────────
#
# The other four tabs (2026-10-12) hold three kinds of row, and the kind decides
# how an empty one is treated: a QUESTION the DD report can answer (the Due
# Diligence tab: "Phase I ESA Completed?") is NEEDS INPUT when unanswered, like
# the Overview; a CHECKLIST step ("LOI Sent", "FIRPTA") reads "Not verified"
# and is never counted as a gap -- the reference sheet had 60 such rows, and
# sixty yellow cells would bury the real ones; a TO-DO (Onboarding) has no
# answer column at all. Names come from the template on every call, so a row
# the team adds to the workbook is fillable without a code change.

TABS = ["Overview", "Legal", "Proforma", "Due Diligence", "Onboarding"]
QUESTION_TABS = {"Overview"}
# The Due Diligence tab answers every row with a verdict, as both team-made
# sheets do, and a subject the report does not cover reads "Not confirmed",
# never NEEDS INPUT (team decision, 2026-10-12: "try to match the original as
# much as possible when it comes to information not provided").
VERDICT_TABS = {"Due Diligence"}
NOT_CONFIRMED = "Not confirmed"
CHECKLIST_TABS = {"Legal", "Proforma"}
NOT_VERIFIED = "Not verified"
# The template's own blank line ("________________"), left as it is.
_BLANK_LINE = re.compile(r"^_{4,}$")


def _tidy(label: str, heading: bool = False) -> str:
    s = re.sub(r"\s+", " ", label).replace("\u2019", "'").strip().rstrip(":").strip()
    s = re.sub(r"^[IVX]+\.\s*", "", s)          # "II. Purchase Agreements"
    if heading and s.isupper():
        s = s.title()                              # "CLOSING", "VERSION 2"; never "IRR"
    return s


_VERSION_ROW = re.compile(r"^\s*VERSION\s+\d+", re.I)


def template_rows(path: Path | None = None) -> list[dict]:
    """
    Every answerable row in the template, in order, as
    {tab, row, label, key, section, kind, has_source}.

    key is what the save tool takes: the plain label on the Overview (as
    before), and "<Tab>: <Section> > <label>" elsewhere, so "Sent" under
    Operating Agreement reads as what it is. An indented label carries the
    unindented row above it ("Legal: Purchase Terms > Escrow Deposit 1 > Due
    Date"); a label repeated inside one section gets " (2)". The Proforma's
    "VERSION n" blocks are sections of their own, ending at the next blank row.
    """
    import openpyxl
    path = path or template_workbook()
    if not path:
        return []
    # Remembered per template file and its last-changed time: a save asks for
    # the rows several times, and an open workbook keeps the file locked on
    # Windows (a check could not delete a template it had just read).
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = None
    if stamp and _ROWS_CACHE.get("stamp") == stamp:
        return [dict(r) for r in _ROWS_CACHE["rows"]]
    wb = openpyxl.load_workbook(path, read_only=False)
    out = []
    for tab in TABS:
        if tab not in wb.sheetnames:
            continue
        ws = wb[tab]
        width_b = (ws.column_dimensions["B"].width if "B" in ws.column_dimensions else None) or 81
        # A to-do tab has no answer column at all (the firm's Onboarding tab is
        # one column of tasks). A width left unset is not the same thing.
        if tab == "Onboarding" or ws.max_column < 2:
            kind = "todo"
        elif tab in QUESTION_TABS:
            kind = "question"
        elif tab in VERDICT_TABS:
            kind = "verdict"
        else:
            kind = "checklist"
        merged_a = {str(m).split(":")[0] for m in ws.merged_cells.ranges}
        has_source_col = (ws.column_dimensions["C"].width or 0) > 12 if "C" in ws.column_dimensions else False
        section, sub, parent, seen = "", "", "", {}
        for r in range(1, ws.max_row + 1):
            a = ws.cell(r, 1)
            raw = a.value
            if raw is None or not str(raw).strip():
                sub, parent = "", ""                 # a blank row ends a VERSION block
                continue
            label = str(raw)
            if tab == "Overview" and (r <= 5 or label.upper().startswith("MINI TERM SHEET")):
                continue
            b_val = ws.cell(r, 2).value
            if _VERSION_ROW.match(label):
                sub, parent = _tidy(label, heading=True), ""
                continue
            is_heading = (f"A{r}" in merged_a) or (
                a.font.b and b_val is None and kind != "todo" and not label.startswith(" "))
            if is_heading:
                section, sub, parent = _tidy(label, heading=True), "", ""
                continue
            indented = label.startswith(" ")
            name = _tidy(label)
            # Inside a VERSION block every row belongs to the version, whatever
            # the indentation says: "Version 2 > IRR", not "Date Drafted > IRR".
            if indented and parent and not sub:
                body = f"{parent} > {name}"
            else:
                body = name
                parent = name
            where = sub or section
            if tab == "Overview":
                key = name
            elif where and where != tab:
                key = f"{tab}: {where} > {body}"
            else:
                key = f"{tab}: {body}"
            if key in seen:
                seen[key] += 1
                key = f"{key} ({seen[key]})"
            else:
                seen[key] = 1
            out.append({"tab": tab, "row": r, "label": name, "key": key,
                        "section": where, "kind": kind, "width": width_b,
                        "has_source": has_source_col and kind == "verdict"})
    wb.close()
    if stamp:
        _ROWS_CACHE.update(stamp=stamp, rows=[dict(r) for r in out])
    return out


_ROWS_CACHE: dict = {}


def row_keys(tab: str | None = None) -> list[str]:
    return [r["key"] for r in template_rows() if tab is None or r["tab"] == tab]


# ─── Saving any tab ─────────────────────────────────────────────────────────

def _chars_per_line(width: float) -> int:
    # About 85 characters fit a column 81 wide on the reviewed sheet.
    return max(30, int((width or 81) * 1.05))


def save_fields(property_name: str, fields: dict, title: str = "", existing: str = "",
                sources: dict | None = None) -> tuple[Path, list[str]]:
    """
    Write answers into any tab, keyed by template_rows() keys. Returns (the
    file, every question row in the workbook still marked NEEDS INPUT).

    A new workbook (no `existing`) is a cleared copy of the template with the
    Overview written. Later calls with `existing` write only the keys given --
    plus, in each tab they touch, a default into every row still empty:
    NEEDS INPUT for a question, "Not verified" for a checklist step. Nothing
    already written is touched, because someone may have edited it. `sources`
    fills the third column on the tabs that have one.
    """
    from config import MINI_TERM_SHEETS_DIR

    rows = {r["key"]: r for r in template_rows()}
    if not rows:
        raise FileNotFoundError("no template workbook in mini_term_sheets/_reference")
    unknown = [k for k in fields if k not in rows] + [k for k in (sources or {}) if k not in rows]
    if unknown:
        raise ValueError("not rows of the template: " + ", ".join(sorted(set(unknown))))
    todo_rows = [k for k in fields if rows[k]["kind"] == "todo"]
    if todo_rows:
        raise ValueError("these rows have no answer column: " + ", ".join(todo_rows))

    if existing:
        path = _find_existing_sheet(property_name, existing)
        book = _Book(path)
    else:
        ref = template_workbook()
        if not ref:
            raise FileNotFoundError("no template workbook in mini_term_sheets/_reference")
        book = _Book(ref)
        # Every answer goes; labels (column A) and the template's blank lines stay.
        for sheet in book.sheets:
            for col, row, attrs, inner in list(book.cells(sheet)):
                if col == "A":
                    continue
                txt = book.cell_text(attrs, inner)
                if txt.strip() and not _is_blank_line(txt):
                    book.set_cell(sheet, f"{col}{row}", None)
        ov = book.sheets["Overview"]
        # Open at the top, where the logo is: the reviewed copy was saved
        # scrolled down to its seventh row.
        xml = re.sub(r'\stopLeftCell="[^"]*"', "", book.text(ov))
        xml = re.sub(r"<selection [^>]*/>", '<selection activeCell="B6" sqref="B6"/>', xml)
        book.put(ov, xml)
        home = property_sheet_folder(property_name)
        home.mkdir(parents=True, exist_ok=True)
        # ONE sheet per property, always this name, replaced by the next run
        # (owner, 2026-10-13: "there should only be one folder where all the
        # output should be in"). Dated and _v2 names had left a property folder
        # a list of look-alikes with nothing saying which was current. Named
        # after the folder, not what the person typed, so "Forney" and "Forney
        # 76" cannot make two files. OneDrive keeps the file's own history.
        path = home / f"{home.name} - Mini Term Sheet.xlsx"
        title_row = next((row for col, row, attrs, inner in book.cells("Overview")
                          if col == "A" and book.cell_text(attrs, inner).strip().upper()
                          .startswith("MINI TERM SHEET")), None)
        if title_row:
            # Same punctuation rule as the answers (team request, 2026-10-08): the
            # template's "MINI TERM SHEET – <NAME>" dash became "<NAME> MINI TERM SHEET".
            # A heading is a name, not a tract list: the pending-deal test passed
            # 70 characters of tracts as the title. Past 45, use the property name.
            heading = title if title and len(title) <= 45 else property_name
            name = re.sub(r"\s*[-–—/]\s*", " ", heading).strip()
            book.set_cell("Overview", f"A{title_row}", f"{name.upper()} MINI TERM SHEET")

    # The tabs this save touches get defaults in their still-empty rows.
    tabs = {rows[k]["tab"] for k in fields} | ({"Overview"} if not existing else set())
    current = {}
    for tab in tabs:
        for col, row, attrs, inner in book.cells(tab):
            if col in ("B", "C"):
                current[(tab, row, col)] = (book.cell_text(attrs, inner), _attr(attrs, "s"))

    def write(r, col, value, gap):
        txt, style = current.get((r["tab"], r["row"], col), ("", ""))
        plain = book.plain_style(style)
        book.set_cell(r["tab"], f"{col}{r['row']}", value,
                      style=book.gap_style(plain) if gap else plain)
        current[(r["tab"], r["row"], col)] = (value, style)

    for key, value in fields.items():
        r = rows[key]
        value = (value or "").strip()
        is_gap = r["kind"] == "question" and (not value or value.upper().startswith(NEEDS_INPUT))
        default = {"question": NEEDS_INPUT, "verdict": NOT_CONFIRMED}.get(r["kind"], NOT_VERIFIED)
        write(r, "B", value or default, is_gap)
    for key, src in (sources or {}).items():
        r = rows[key]
        if r["has_source"] and (src or "").strip():
            write(r, "C", src.strip(), False)
    for r in template_rows():
        if r["tab"] not in tabs or r["kind"] == "todo" or r["key"] in fields:
            continue
        txt, _ = current.get((r["tab"], r["row"], "B"), ("", ""))
        if txt.strip():
            continue                                   # written earlier, or a blank line
        if r["kind"] == "question":
            write(r, "B", NEEDS_INPUT, True)
        elif r["kind"] == "verdict":
            write(r, "B", NOT_CONFIRMED, False)
        else:
            write(r, "B", NOT_VERIFIED, False)

    # Excel does not grow a row whose height is set, so each written row is
    # sized to its text, the way the reviewed sheet was sized by hand.
    answer_rows = {(r["tab"], r["row"]) for r in rows.values()}
    for (tab, row, col), (txt, _) in current.items():
        if col != "B" or (tab, row) not in answer_rows:
            # Only the answer rows. The Overview's first rows hold the logo,
            # 187 points tall in the template; sizing every row with a B cell
            # set them to 17.85 and squashed it (owner, 2026-10-12).
            continue
        width = next((r["width"] for r in rows.values() if r["tab"] == tab), 81)
        src, _ = current.get((tab, row, "C"), ("", ""))
        per = _chars_per_line(width)
        lines = max(sum(max(1, -(-len(p) // per)) for p in (txt or " ").split("\n")),
                    sum(max(1, -(-len(p) // 44)) for p in (src or " ").split("\n")))
        book.set_row_height(tab, row, max(17.85, round(lines * 13.6 + 4, 2)))

    gaps = []
    for r in template_rows():
        if r["kind"] != "question":
            continue
        for col, row, attrs, inner in book.cells(r["tab"]):
            if col == "B" and row == r["row"] and \
                    book.cell_text(attrs, inner).strip().upper().startswith(NEEDS_INPUT):
                gaps.append(r["key"])
    book.save(path)
    return path, gaps


def current_values(property_name: str, existing: str, tab: str) -> list[tuple[str, str]]:
    """
    (row key, what the saved sheet says now) for one tab, read without
    changing the file. The Legal fill must add to what the DD report wrote, and
    without this it could only see the row names (Option A test, 2026-10-12).
    """
    rows = [r for r in template_rows() if r["tab"] == tab and r["kind"] != "todo"]
    book = _Book(_find_existing_sheet(property_name, existing))
    now = {row: book.cell_text(attrs, inner) for col, row, attrs, inner in book.cells(tab)
           if col == "B"}
    return [(r["key"], (now.get(r["row"]) or "").strip()) for r in rows]


def save_overview(property_name: str, fields: dict, title: str = "",
                  existing: str = "") -> tuple[Path, list[str]]:
    """The Overview tab only; kept for the checks and older callers."""
    bad = [k for k in fields if k not in OVERVIEW_FIELDS]
    if bad:
        raise ValueError(f"not Overview rows: {', '.join(bad)}")
    return save_fields(property_name, fields, title=title, existing=existing)


# ─── Reading the report for one tab ─────────────────────────────────────────
#
# One call per tab rather than one briefing for all five: a single briefing
# for 168 rows would run past what a tool call should carry, and a tab is the
# unit a person checks. Keyed by (tab, section) so a section the team adds to
# the template simply gets no passages, never a crash.

_SECTION_QUERIES = {
    ("Legal", "Purchase Agreements"): "letter of intent purchase agreement amendment drafted sent executed effective date",
    ("Legal", "Purchase Terms"): "earnest money deposit feasibility period inspection closing days extension seller materials",
    ("Legal", "Site & Ownership"): "title commitment vested encumbrance easement lien legal access frontage survey boundary acres",
    ("Legal", "Operating Agreement"): "partnership agreement operating agreement executed amendment general partner limited partners",
    ("Legal", "Closing"): "closing settlement statement deed affidavit assignment closing date recorded",
    ("Proforma", "Budgets"): "entitlement budget cost to complete estimate engineer opinion of probable cost",
    ("Proforma", "Version"): "pro forma assumptions sale price entitlement period commission acquisition fee disposition fee "
                             "management fee operating costs property tax preferred return priority return waterfall "
                             "profit split IRR multiple acquired purchase price note loan road cost share development agreement",
    ("Proforma", "Proforma"): "critical due diligence priority gates must confirm before",
    ("Due Diligence", "Environmental"): "Phase I environmental site assessment wetlands floodplain FEMA endangered species contamination",
    ("Due Diligence", "Zoning & Entitlements"): "zoning rezoning planned development general plan comprehensive plan permitting pathway opposition political risk",
    ("Due Diligence", "Utilities & Infrastructure"): "water provider capacity sewer power electric gas pipeline fiber road access rail "
                                                     "road improvements offsite costs stormwater drainage",
    ("Due Diligence", "Market & Economic Context"): "absorption comparable land sales nearby projects competitive incentives workforce housing",
    ("Due Diligence", "Reports & Studies"): "geotechnical soils topographic survey traffic impact study drainage study capacity letters",
    ("Due Diligence", "Permitting Pathway"): "permit local state federal approvals opposition",
    ("Due Diligence", "Outstanding Items / Action List"): "outstanding items action list next steps gates confirm obtain",
}
_PASSAGES_PER_SECTION = 3
_TAB_CAP = 22000

# The Due Diligence tab is searched ROW BY ROW, one passage each (2026-10-12).
# One broad search per section left ten utility rows sharing three passages:
# measured on the first test report, 5 of 14 key facts reached the tab and the
# text hit its cap; per row, 13 of 14 in about the same length. A label alone
# is a weak search for a few rows, so these add the words a report uses.
_ROW_HINTS = {
    "phase i esa completed": "phase i environmental site assessment recognized environmental conditions",
    "phase ii esa": "phase ii environmental site assessment depends on phase i",
    "pad / pud": "planned development overlay flexibility pd zoning",
    "comparable land sales": "comps land valuation land sales closed sold acres per square foot",
    "topographic survey": "topography elevation land title survey",
    "current zoning": "zoned zoning district agricultural",
    "rezoning required": "rezoning required planned development",
    "general plan": "comprehensive plan character area future land use designation",
    "drainage / flood": "floodplain modeling hydraulic drainage study",
    "rail": "rail railroad",
    "power": "electric power substation provider",
    "natural gas": "natural gas utility service retail provider",
    "fiber": "fiber telecom broadband",
    "offsite costs": "offsite cost estimate infrastructure cost to complete",
    "absorption": "absorption days on market inventory vacancy",
    "nearby projects": "nearby projects anchors competing development",
    "incentives": "incentives chapter 380 economic development abatement",
    "workforce": "median income population households workforce",
    "outstanding item": "outstanding items priority gates confirm obtain",
}
_ROW_TAB_CAP = 30000

# The checklist tabs (2026-10-12) keep their section search and add a row-by-row
# search on top, GATED: a row gets a passage only when the passage names the
# row's own subject (two words of four letters or more from the label and its
# helper words). Without the gate "LOI Sent", "FIRPTA" or "593C" would grab
# whatever paragraph scored highest. Measured on the reference report: the
# section search alone missed the frontage and legal description paragraphs,
# and caught the partnership agreement on one run and not the next; with the
# row search added all five reached the tab, for about 4,800 more characters.
# On the pending-deal report, which never mentions a purchase agreement,
# deposits or closing, three rows qualified, which is correct.
_CHECKLIST_HINTS = {
    "loi": "letter of intent loi",
    "psa": "purchase agreement purchase and sale contract",
    "ammendment": "amendment purchase agreement",
    "amendment": "amendment purchase agreement",
    "escrow deposit": "earnest money deposit escrow",
    "feasibility": "feasibility inspection period",
    "seller materials": "seller deliveries materials",
    "title commitment": "title commitment vested record title",
    "encumbrances": "lien easement encumbrance deed of trust",
    "legal access": "frontage access driveway",
    "survey / boundary": "survey boundary acreage land title survey",
    "operating agreement": "partnership agreement operating agreement amendment executed general partner",
    "acc filing": "certificate formation secretary of state filing",
    "settlement statement": "settlement statement closing statement",
    "description of real property": "legal description survey abstract tract recorded",
    "warranty deed": "deed recorded volume page grantor",
    "affidavit": "affidavit",
    "school agreement": "school district agreement development agreement",
    "utility agreement": "utility agreement",
    "pada or da": "development agreement",
    "closing date": "acquired closed closing",
    # Proforma rows (2026-10-12): a row called "Assumptions" or "IRR" has no
    # words of its own to search for, so the first pass filled one row of nine.
    "entitlement budget": "entitlement budget cost estimate engineer opinion of probable cost",
    "costs to compete": "cost to complete estimate infrastructure bids contractor",
    "date drafted": "pro forma dated drafted underwriting model version",
    "assumptions": "acquired purchase price basis financing loan note lien deed of trust refinance "
                   "payoff maturity interest rate assumptions",
    "entitlement period": "entitlement timeline schedule period months approvals zoning process",
    "sale price": "sale price disposition asking price offer listed exit value",
    "sale date": "sale closing date disposition target timing",
    "commision": "brokerage commission broker fee listing agreement",
    "commission": "brokerage commission broker fee listing agreement",
    "fee rates": "acquisition fee disposition fee management fee partnership agreement general partner",
    "operating costs": "property taxes assessed value tax rate annual taxes holding costs appraisal district",
    "unusual operating": "development agreement cost share road costs obligation payments assessment lien",
    "pref rate": "preferred return priority return cumulative annual capital contributions",
    "profit split": "waterfall distributions percentage interests partners split",
    "irr": "internal rate of return IRR returns pro forma",
    "cash multiple": "equity multiple cash multiple returns pro forma",
    "critical due diligence": "priority diligence gates critical due diligence must confirm reconcile",
}
# A passage for these rows must also carry one of the row's own phrases. The
# two-word gate alone let "Sale Price" take a list of housing developments and
# "Pref Rate" a retail capital-market note out of a 160,000-character report.
_ROW_ANCHORS = {
    "entitlement budget": ["entitlement budget", "budget"],
    "costs to compete": ["cost to complete", "costs to complete", "opinion of probable cost", "cost estimate"],
    "date drafted": ["pro forma dated", "proforma dated", "model dated", "underwriting dated"],
    "assumptions": ["loan", "note", "lien", "financing", "refinanc", "purchase price", "acquired"],
    "entitlement period": ["entitlement timeline", "entitlement schedule", "entitlement period",
                           "entitlement process"],
    "sale price": ["asking price", "sale price", "list price", "disposition price", "offer of", "offered at",
                   "under contract at", "contract price"],
    "sale date": ["closing date", "sale date", "target close"],
    "commision": ["commission rate", "commission of", "% commission", "brokerage commission", "broker fee"],
    "commission": ["commission rate", "commission of", "% commission", "brokerage commission", "broker fee"],
    "fee rates": [" fee"],
    "operating costs": ["property tax", "assessed", "tax rate", "appraisal district", "taxes of", "annual taxes",
                        "tax certificate"],
    "unusual operating": ["development agreement", "cost share", "cost-share", "road cost", "assessment"],
    "pref rate": ["preferred return", "priority return", "pref "],
    "profit split": ["waterfall", "distribution", "percentage interest"],
    "irr": ["irr", "internal rate"],
    "cash multiple": ["equity multiple", "cash multiple", "moic"],
    "critical due diligence": ["diligence"],
}
_GATE_SKIP = {"drafted", "sent", "received", "comments", "final", "draft", "executed", "date",
              "dates", "period", "paid", "due", "see", "checklist", "section", "the", "and", "for",
              "redlines", "out", "signature", "verified", "day", "summary", "legal", "version",
              "with", "from", "this", "that"}


def _row_evidence(text: str, rows: list[dict]) -> str:
    """
    The best passage for each row, grouped by section. When a row's best
    passage was already sent for an earlier row, it points there instead of
    taking the next-best: that gave one row a paragraph about drive-through
    pads for "PAD / PUD Flexibility", and another the council members' names.
    """
    from corpus.find import find
    used, parts, section = {}, [], None
    for r in rows:
        low = r["label"].lower()
        hint = next((h for k, h in _ROW_HINTS.items() if k in low), "")
        query = re.sub(r"\(.*?\)|[?/]", " ", r["label"]) + " " + hint
        found, _ = find(text, query, 1)
        if r["section"] != section:
            section = r["section"]
            parts.append(f"== {section} ==")
        if not found:
            parts.append(f"[for: {r['label']}] (no passage matched)")
            continue
        f = found[0]
        key = f["text"][:200]
        if key in used:
            parts.append(f"[for: {r['label']}] (see the passage for {used[key]} above)")
            continue
        used[key] = r["label"]
        parts.append(f"[for: {r['label']}] ({f['heading']})\n{f['text']}")
    out = "\n\n".join(parts)
    if len(out) > _ROW_TAB_CAP:
        out = out[:_ROW_TAB_CAP] + "\n[... passages cut here to keep this short ...]"
    return out


def _gated_row_evidence(text: str, rows: list[dict], seen: set) -> list[str]:
    """Row-by-row passages for a checklist tab, only where the passage names the row's subject."""
    from corpus.find import find
    out = []
    for r in rows:
        low = r["label"].lower()
        where = (r["section"] or "").lower()
        hint = " ".join(h for k, h in _CHECKLIST_HINTS.items() if k in low or k in where)
        words = set(re.findall(r"[a-z]{4,}", low + " " + hint)) - _GATE_SKIP
        anchors = [a for k, v in _ROW_ANCHORS.items() if k in low or k in where for a in v]
        found, _ = find(text, re.sub(r"\(.*?\)|[?/]", " ", r["label"]) + " " + hint,
                        5 if anchors else 1)
        f = None
        for cand in found:
            body = (cand["heading"] + " " + cand["text"]).lower()
            if len([w for w in words if w in body]) < 2:
                continue
            if anchors and not any(a in body for a in anchors):
                continue
            f = cand
            break
        if f is None:
            continue
        key = f["text"][:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(f"[for: {r['label']}] ({f['heading']})\n{f['text']}")
    return out


def tab_evidence(rel_path: str, tab: str) -> tuple[str, dict]:
    """The report passages most likely to fill one tab, grouped by section."""
    from corpus.find import find
    text, meta = read_report(rel_path)
    if text.startswith(("[Cannot read", "[Could not read")):
        return text, meta
    text = _clean(text)
    tab_rows = [r for r in template_rows() if r["tab"] == tab]
    if tab_rows and tab_rows[0]["kind"] == "verdict":
        out = _row_evidence(text, tab_rows)
        meta["sent_chars"] = len(out)
        return out, meta
    sections = []
    for r in template_rows():
        if r["tab"] == tab and r["section"] and r["section"] not in sections:
            sections.append(r["section"])
    parts, used = [], set()
    for section in sections:
        key = (tab, "Version" if section.lower().startswith("version") else section)
        query = _SECTION_QUERIES.get(key)
        if not query or (key[1] == "Version" and section.lower() != "version 1"):
            continue
        found, _ = find(text, query, 8)
        keep = []
        for p in found:
            k = p["text"][:200]
            if k in used:
                continue
            used.add(k)
            keep.append(f"({p['heading']})\n{p['text']}")
            if len(keep) == _PASSAGES_PER_SECTION:
                break
        if keep:
            parts.append(f"== {section} ==\n" + "\n\n".join(keep))
    if tab_rows and tab_rows[0]["kind"] == "checklist":
        extra = _gated_row_evidence(text, tab_rows, used)
        if extra:
            parts.append("== Row by row ==\n" + "\n\n".join(extra))
    out = "\n\n".join(parts)
    if len(out) > _TAB_CAP:
        out = out[:_TAB_CAP] + "\n[... passages cut here to keep this short ...]"
    meta["sent_chars"] = len(out)
    return out, meta


# Which documents can fill a section's missing rows, for the "may I look in
# the folder?" step on the other tabs.
SECTION_SOURCES = {
    ("Legal", "Purchase Agreements"): ["Letter of intent", "Purchase agreement"],
    ("Legal", "Purchase Terms"): ["Purchase agreement", "Settlement statement"],
    ("Legal", "Site & Ownership"): ["Title commitment", "Deed", "Reports and studies"],
    ("Legal", "Operating Agreement"): ["Partnership / operating agreement", "Partnership resolution or consent",
                                       "Entity status certificate"],
    ("Legal", "Closing"): ["Settlement statement", "Deed", "Purchase agreement"],
    ("Proforma", "Budgets"): ["Budget or cost estimate"],
    ("Proforma", "Version"): ["Firm investment materials", "Partnership / operating agreement", "Budget or cost estimate"],
    ("Due Diligence", "Environmental"): ["Reports and studies"],
    ("Due Diligence", "Utilities & Infrastructure"): ["Reports and studies"],
    ("Due Diligence", "Reports & Studies"): ["Reports and studies"],
    ("Due Diligence", "Market & Economic Context"): ["Offering memorandum / broker package", "Firm investment materials"],
}


def kinds_for_keys(keys: list[str]) -> list[str]:
    """The document kinds that can fill the given rows, Overview rows included."""
    rows = {r["key"]: r for r in template_rows()}
    kinds = []
    for k in keys:
        r = rows.get(k)
        if not r:
            continue
        if r["tab"] == "Overview":
            found = FIELD_SOURCES.get(k, [])
        else:
            sec = "Version" if r["section"].lower().startswith("version") else r["section"]
            found = SECTION_SOURCES.get((r["tab"], sec), [])
        kinds += [x for x in found if x not in kinds]
    return kinds
