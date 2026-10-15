# CLAUDE.md — Mini term sheets

Guidance for Claude Code when changing the mini term sheet tool. Read this before
touching anything in `system/term_sheet/`, the four tools in `mcp_server.py`
(`start_mini_term_sheet`, `get_mini_term_sheet_tab`, `save_mini_term_sheet`,
`check_mini_term_sheet_documents`), or §11 of `scripts/check_portfolio_comparison.py`.
The project-wide rules in the root `CLAUDE.md` still apply; this file adds what is
specific to term sheets.

This repository is public. Never write a real property name, party, price, acreage
or file name into this file, the code or the checks. Say "the reference property" or
"the pending-deal test" instead. The leak guard blocks a commit that does.

**Shipped as one piece on 2026-10-15**, after the owner's rule of 2026-10-12 that nothing
from this work ships until the whole project is done (all five tabs and the dashboard).
The release steps are in the `publish-release` skill. At release the deterministic checks
passed (§11, 237 checks, and `check_term_sheet_page.py`), and the owner confirmed the tool
works from their own use and waived the separate blind end-to-end run.

## Build rules (for code, schema and template work)

1. **Think first.** State assumptions, present competing interpretations, and ask
   before building when something is unclear. Say so if a simpler approach exists.
2. **Simplicity.** Write the minimum code that solves the request. Add no speculative
   features. Exception: a design the team has explicitly asked for, such as a core plus
   deal-type module schema, is built as specified once its scope is confirmed with the
   team. It is never started on speculation.
3. **Surgical changes.** Touch only what the request needs. Do not rename template
   rows, restyle the workbook, or refactor working code unless asked. Mention unrelated
   problems instead of fixing them.
4. **Verify against the gold standard.** Every phase has a check, and the main one is a
   blind regeneration of the reference property's term sheet from its DD report,
   compared tab by tab and row by row with the team's agreed sheet for that property
   (see "Checks" below). Report what matches, what differs, and why. A change to how
   pending deals are handled is also checked against the pending-deal test.
5. **For multi-step work, state the plan first:**
   1. [step] → verify: [check]
   2. [step] → verify: [check]

## What this is

A teammate asks Claude Desktop for "a mini term sheet for <property>". The agreed flow
(2026-10-08, widened to all tabs 2026-10-12):

1. **The report.** The teammate names the DD report, drops it in the team folder's
   `mini_term_sheets/DD Drop`, or asks the tool to find it in the property's folder.
   More than one: Claude asks which. A report attached to the chat is not read from the
   chat; Claude asks for it to be dropped instead (tens of thousands of tokens saved).
2. **The Overview, from the DD report only.** A short plain summary: six fields
   explained in a short paragraph (Owner, Legal Description, Parcel Size, Proposed Use,
   Purchase Price, Price per Acre), seven facts only, Seller Contact contact details
   only. Saved into the team folder in the firm's template. What the report lacks is
   NEEDS INPUT.
3. **The other tabs, one call each.** `get_mini_term_sheet_tab` returns a tab's rows
   (named from the template) and the report passages for them; Claude saves the tab and
   moves to the next: Due Diligence, Legal, Proforma. Onboarding is a to-do list and is
   never written. Still the DD report only.
4. **"May I look for what is missing?"** Only on a yes, and only for the rows marked
   NEEDS INPUT: `check_mini_term_sheet_documents(purpose="missing")` lists just the
   documents that can fill them (`FIELD_SOURCES` for the Overview, `SECTION_SOURCES`
   for the other tabs).
5. **"Shall I confirm the numbers?"** Only on a yes:
   `check_mini_term_sheet_documents(purpose="numbers")`. Any number that disagrees with
   the signed documents goes back to the person; nothing changes until they answer.

Next, not built yet: a single-file dashboard per property built from the same facts,
with the workbook as its Excel export.

## Where things live

| What | Where | Changes by |
|---|---|---|
| The machinery: finding, reading, naming rows, writing the workbook | `system/term_sheet/sheet.py` | Code change, published update |
| The four tools Claude Desktop calls, and the steps they hand Claude | `system/mcp_server.py` | Code change, published update |
| DD reports handed over without filing them first | Team folder: `mini_term_sheets/DD Drop/` | People, by dropping a file |
| How a sheet is WRITTEN: tone, field contents, the other tabs, abbreviations | Team folder: `mini_term_sheets/_reference/Mini Term Sheet Rules.md` | Editing that document. No update, no restart |
| The template: logo, layout, labels, and therefore the ROWS that can be filled | Team folder: `mini_term_sheets/_reference/Mini Term Sheet Template.xlsx` (fixed name) | Replacing that one workbook |
| The wording example: the level of detail Claude copies | Team folder: any other `.xlsx` in `_reference` (newest wins) | Replacing that workbook |
| Finished sheets | Team folder: `mini_term_sheets/<property's drive folder name>/` | People, by hand |
| Regression checks | `scripts/check_portfolio_comparison.py` §11 | Code change |

**Writing rules belong in the team folder's rules document, not in code.** The team
expects to keep refining how sheets read, and a rule kept in code needs a published
update and a Claude Desktop restart on every machine. `DEFAULT_RULES` in `sheet.py`
is only the fallback when that document is missing or broken, and the text it was
first seeded from. When you change a writing rule, change the document; change
`DEFAULT_RULES` only to keep the fallback in step, and say so. Before overwriting the
team's document, compare it with what the tool last wrote; a person may have edited it.

**Rows come from the template, never from code.** `template_rows()` reads every tab of
the template on each call (cached by the file's last-changed time) and names each
answerable row: the plain label on the Overview, `"<Tab>: <Section> > <label>"`
elsewhere, with the parent for an indented label and " (2)" for a repeat. A row the
team adds to the workbook is fillable with no code change. Four kinds of row: a
**question** (Overview; empty → NEEDS INPUT, a gap), a **verdict** (Due Diligence;
empty → "Not confirmed", never a gap), a **checklist** step (Legal and Proforma; empty →
"Not verified", never a gap), and a **to-do** (a tab with no answer column; never
written).

## Decisions that must not change without asking

Each of these was decided with the team, most after a test showed the alternative
going wrong.

- **The sheet mirrors the DD report; it does not research** (owner, 2026-10-12). Every
  tab says only what the report says. Missing or wrong information is fixed in the DD
  report, and the next sheet picks it up. The "look in the folder" question stays only as
  an optional quick lookup (a seller's contact details and the like), never as the way
  gaps are meant to be closed. Checking numbers against the signed documents is
  verification, not research, and stays.
- **The Legal checklist may be filled from the deal file, only on the person's yes**
  ("Option A", owner, 2026-10-12, kept after a test). Offered once, with the closing
  questions; never part of the first pass; the Proforma is never filled this way.
  `check_mini_term_sheet_documents(purpose="legal", deal_year=...)` does the reading on
  the server and hands over everything in ONE reply: the deal's papers (at most eight,
  `_LEGAL_READ`), each asked the questions a reader asked by hand in the first test
  (`_LEGAL_QUESTIONS`), with the matching passages; then every Legal row with what it
  says now (so the DD report's text is added to, never replaced) and the deal's files
  whose names match the row, which is what "Signed copy on file" and "Not provided" rest
  on. Three calls instead of ten; measured 29,000 characters against 69,000.
  **The deal is the YEAR the DD report gives, never a folder branch.** A folder called
  "Acquisition" is one layout; on two other properties checked the same day the purchase
  papers sat across sibling folders and the only branch holding a purchase agreement by
  name was a later sale. Files within a year of that year are the deal; the firm's own
  sales ("Disposition", "!RESALES") are left out, the one folder-name rule kept because
  it only removes papers. No year, or no papers in it: the reply lists the years that
  hold purchase papers and Claude asks; it never picks. One copy of the agreement itself
  is read, then its amendments newest first (two copies of one amendment once took the
  agreement's slot). Reading stops between documents at `LEGAL_READ_BUDGET_SECONDS` and
  a second call finishes, since scanned pages are remembered. **A date is written only
  if a passage states it or the file's own name prints it**: the first test wrote a
  file's saved date as a signing date.
- **Rows are short, and the logo keeps its size (owner, 2026-10-12).** Every row is one
  paragraph of two to four sentences, as the team's sheets are; the Proforma's Assumptions
  and Notable expenses may run to a long paragraph. A document's facts are folded into the
  row, never appended as a second paragraph. `too_long()` flags a row past the longest the
  team's own sheets run (`ROW_LENGTH_CAP`: 700 on Legal and Due Diligence, 1,400 on the
  Proforma, none on the Overview), and the save reply asks for a rewrite. Row heights are
  set only on the template's ANSWER rows: sizing every row holding a B cell set the
  Overview's logo row from 187 points to 17.85 and squashed the logo on every sheet made
  until then.
- **End-to-end run on the pending-deal report (2026-10-13), and what it fixed.** 28 tool
  calls, about 11 minutes, 290,000 characters of replies; the sheet matched the team's own
  May sheet on all 14 Overview rows and 30 of 38 Due Diligence rows (the September report is
  an engineering review and does not cover Phase I, wetlands or geotechnical), and filled 39
  Legal rows against the May sheet's 5. Found by that run: `find_in_document` could not open a
  report in the DD Drop (every follow-up question failed; it now resolves drop paths); the
  numbers check with no year took the newest agreement on file, a DRAFT of the firm's own
  later sale, as its authority (a year is now required, as for the Legal fill, and the names of
  the parties are listed for checking too); the start steps offered the dashboard before the
  fills (reordered: questions, numbers, dashboard last); "Assignment of School Agreement"
  matched an infrastructure agreement by name (generic words no longer count). The run also
  surfaced a real discrepancy for a person: the DD report and the draft deed name a different
  buyer than the signed agreement.
- **The numbers check covers every tab, on the person's yes (owner, 2026-10-12).**
  `purpose="numbers"` lists every dollar figure, acreage and parcel number on every tab with
  its row (`sheet_figures`), reads the acquisition's agreement, settlement statement and deed
  for the report's year plus the newest title commitment and tax record (`numbers_documents`,
  the same reading engine), and hands both over in one reply. A figure no passage mentions is
  not a disagreement; a disagreement goes to the person as one list before anything changes.
  Measured on the reference sheet: 27 rows of figures, six documents, 16,000 characters, 7s.
- **The dashboard is a VIEW of the workbook, rebuilt on every save (2026-10-12).**
  `dashboard.py` reads the saved workbook and fills `dashboard_template.html`, written beside
  the workbook as `<same name>.html`; `save_mini_term_sheet` rebuilds it after every save and
  `open_mini_term_sheet_dashboard` opens it. One source: the page can never disagree with the
  Excel file, and "export to Excel" is the file that already exists. Nothing on the page is
  worked out: figures and dates are read from cells (the first dollar figure, the first acreage,
  dates in the row text), and the two derived things, days counted from today and the tally of
  verdicts, say so on the page. The style follows the example the owner shared (slate header,
  gold rule, serif headings, mono figures, light and dark), with no external fonts or scripts,
  because the page opens from the team folder with no network. Seven tabs: Overview (figure
  cards, key dates, deal at a glance, needs attention, how complete), Timeline (every date on the
  Legal tab), Site & Title, Due Diligence (verdict counts, folded by section with the source
  column), Legal and Proforma (folded, empty steps greyed), Onboarding. A search box covers every
  row of every tab. The copied summary always appears in a box, since headless Edge never answers
  a clipboard request. For an owned property the key dates are the purchase's, earliest first;
  the first build led with the newest title commitment, which is not a key date of anything.
  `scripts/check_term_sheet_page.py` drives a page in headless Edge; §11 asserts a page is built
  beside the synthetic workbook with no external resources. It holds real deal data and is never
  published as a web page.
- **The Onboarding tab is a one-column to-do list and is never written.** The reviewed
  sheet's copy is identical to the template's: nine finance tasks, no answer column.
- **A sheet is made only when someone asks for one, for a named property.** Never on
  Claude's initiative and never for several properties at once.
- **Several DD reports: ask, never choose.** The tool lists them and Claude asks which.
- **One sheet per property, one name, replaced by the next run (owner, 2026-10-13).**
  `<folder name> - Mini Term Sheet.xlsx` and its `.html` beside it, nothing else in the
  folder: no dated names, no `_v2`, no `_previous/`. The owner found a property folder full of
  look-alikes with nothing saying which was current. Named after the FOLDER, not what the
  person typed, so "Forney" and "Forney 76" cannot make two files. A later fill edits the
  file in place; a new DD report replaces it; OneDrive keeps the file's own history.
- **One folder per property** inside `mini_term_sheets`, named after the property's
  folder on the drive, so a short name, a longer one and the formal name all land in
  the same place. A deal with no drive folder uses the name as typed.
- **A new sheet never overwrites an earlier one** (`_v2`, `_v3`). Filling in answers
  afterwards (`existing_file`) changes only the cells given, because someone may have
  edited the file. A tab save also writes a default into that tab's still-empty rows,
  and nothing else.
- **Every sheet is copied from one fixed template,** `Mini Term Sheet Template.xlsx`,
  kept apart from the wording example. When one file served as both, a blind test that
  swapped in another property's sheet as the example also moved the Vaulter logo, and
  anyone dropping a sheet into `_reference` would have changed every sheet's look.
- **The workbook is edited, never rebuilt.** Every part of the template is copied byte
  for byte and only the answer cells change. A library that rebuilds the file (the
  first version used openpyxl to save) moved, enlarged and outlined the logo. Do not
  reintroduce one for writing. Reading with openpyxl is fine, and every workbook opened
  for reading is closed, because an open one keeps the file locked on Windows.
- **No answer from the template survives into a new sheet,** including the hidden copy
  in the file's shared-string list. A cleared answer left there would carry one
  property's facts inside another property's file. The template's own blank lines
  ("________________") are kept.
- **The heading is "<SHORT NAME> MINI TERM SHEET",** with no dash. A title over 45
  characters falls back to the property name.
- **Gaps are marked, never guessed.** An unestablished question row is saved as NEEDS
  INPUT, highlighted, and the teammate gets one list at the end, once every tab is
  saved. A checklist step the report does not mention reads "Not verified" and is not a
  gap: the reference sheet had sixty such rows, and sixty yellow cells would bury the
  real ones.
- **The Due Diligence tab answers with a verdict line first** ("Not completed", "Low
  risk"), then one or two sentences, with where it came from in the third column. Both
  team-made sheets were written that way. **It never shows NEEDS INPUT** (owner,
  2026-10-12: "match the original as much as possible when it comes to information not
  provided"): when the report says a thing is not done or not known, that is the
  answer, with its reason; when the report says nothing, the row reads only a verdict
  such as "Not confirmed", as the reference sheet's own water-supply row does. A
  requirement named after another state is answered the same way, never "not
  applicable".
- **Price per Acre is confirmed by a person when the report prints no per-acre
  figure** (owner, 2026-10-12). Claude does not calculate one unasked; it leaves the row
  for input and proposes the calculation in the questions at the end.
- **The DD report first; everything else only with the person's yes.** The sheet is
  built from the report alone, all tabs. Then Claude asks before looking for missing
  rows (missing rows only), and asks again before confirming numbers, where any
  disagreement goes back to the person. Settling everything up front cost tens of
  thousands of tokens and over ten minutes a sheet, which the team called too long. Do
  not move any lookup back in front of the question without asking.
- **A short plain summary of the report, not a copy of it.** Six Overview fields
  explained in a short paragraph, seven facts only, Seller Contact contact details only,
  modelled on where the first full tool sheet explained and where it stated facts. An
  earlier "never shorten anything" rule produced sheets twice the length; the DD report
  is where the detail lives.
- **Professional term-sheet tone, without abbreviations or fussy punctuation.**
  Standard real estate terms stay; unexplained abbreviations, semicolons, brackets and
  slashes do not. The save tool reports breaches back for correction. **There is no
  rule against repeating words:** one was tried and removed because on legal fields it
  forced the documents' own terms ("recorded", "easement") out of the sheet.
- **Owned or being acquired is decided by what the DD report says,** not by the
  folder or the Project Master stage. The status line only suggests; a pending folder
  or the Acquisition stage is never announced as owned.
- **Proposed Use(s) is what the DD report says about the strategy,** as it says it:
  its recommendation, or the options it compares. Nothing else is used for this field.
  It is left for the person only when the report says nothing.
- **Read only what is needed.** Passages per field (Overview), per row (Due
  Diligence) or per section (Legal, Proforma), one tab per call, never whole reports. A
  tab may ask the report at most two follow-up questions. The Due Diligence tab is
  searched row by row because one search per section reached 5 of 14 key facts on the
  reference report and per row reaches all of them; a row whose best passage was
  already sent points to it rather than taking a worse one. The Legal and Proforma tabs keep their
  section search and add a row-by-row search GATED on the passage naming the row's own
  subject, so checklist rows like "FIRPTA" get nothing rather than a stray paragraph. On
  the reference report the section search alone caught the partnership agreement on one
  run and not the next, and never reached the frontage or legal description. The tab calls carry only the parts of the rules they need. Never
  read email (`.msg`); that is outside this system by firm policy.
- **The property folder is listed directly, not from the nightly file list,** which
  can be days old. Names only; nothing is downloaded until Claude chooses a document.
- **A term sheet is never a source for a term sheet,** and copies marked not used,
  superseded or void are skipped.

## Checks

Run after any change here, before saying it works.

1. **The regression suite:** `python scripts/check_portfolio_comparison.py`. Section
   §11 covers term sheets with a made-up five-tab template only. It must stay fully
   green. When a check fails, read the check before assuming the code is wrong: the
   catch-all names the failing line.
2. **The gold-standard blind test** (the main check, build rule 4). Test the first
   pass on its own, and the document check separately when it changes:
   - Run the tools exactly as Claude Desktop would, through a small harness that calls
     `create_mcp_server()` and points `config.MINI_TERM_SHEETS_DIR` at a scratch
     folder, so nothing lands in the team folder.
   - Use a fresh subagent that sees only what the tools return. Forbid it from opening
     any term sheet, the `_reference` folder, or `Downloads`.
   - **The example it learns from must not be the sheet it is graded against.** For
     the reference property, point `config.MINI_TERM_SHEETS_REFERENCE_DIR` at a folder
     holding another property's sheet as the example, plus copies of the rules
     document and of `Mini Term Sheet Template.xlsx`. Without the template copy the
     test sheet takes the example's layout and logo position, which is a test artifact,
     not a fault. The DD Drop folder moves with `MINI_TERM_SHEETS_DIR`, so a dropped
     report is not found by a redirected harness; name the report by its library path.
   - Compare the saved sheet, tab by tab and row by row, with the team's agreed sheet
     for the reference property in its property folder. Report matches, differences
     and why, and separate what is newer information from what is wrong.
3. **The pending-deal test,** for anything touching owned versus being acquired,
   purchase agreements or folder finding: the same blind run on a deal filed in a
   pending folder, graded against its documents.
4. **The leak list,** before committing: no real names or figures in code, checks or
   this file.

Things that have gone wrong here and are worth checking for:

- Shell heredocs eat backslashes. A `\b` in a regular expression written through one
  became an invisible control character in 14 places, and a `"\n"` inside a heredoc
  became a real line break and broke the module. Check `chr(8) not in sheet.py`; §11
  does. Prefer a patch script written with the file tool.
- A patch script that builds the new text and forgets to write the file reports "ok"
  and changes nothing. Compile and grep for the new name after every patch.
- Two helpers once shared a name and the second silently replaced the first, which
  would have broken report reading. Search for a name before adding it.
- A template column with no set width is not a to-do column. A tab is a to-do only
  when it has no answer column at all; an earlier rule read the synthetic template's
  Overview as a to-do list and refused every save.
- Excel row heights are set in the template and do not grow, so the save step sizes
  each answer row to its text, per column width, and to the source column too.
- Several kinds of document are filed by folder rather than by name ("SS" for a
  settlement statement, "Commit" for a title commitment), so matching looks at the
  folder as well as the file name.

## Known limits

- Scanned survey drawings often yield no readable text, so a survey's area table may
  be unusable; say so rather than guess.
- A deal folder nested deeper than `<STATE>/<Property>` or one level inside a `!`
  grouping folder is not found by name.
- The document check costs far more than the first pass, because it reads documents
  beyond the report. A deal with many conflicts can take twice the reading of a clean
  one. That is why it runs only when asked.
- In the first pass, a DD report's own errors pass into the sheet unchallenged. On the
  reference property the report and the title commitment both carried acreage and
  price figures that the recorded documents contradict; only the document check finds
  those.
- The Legal and Proforma tabs are mostly checklist steps a DD report rarely states, so
  on an owned property most of their rows read "Not verified" after the first pass.
  The optional Legal fill closes most of that tab; the Proforma stays as the report left it.
- The Proforma's rows are named for what they hold, not for words a report uses
  ("Assumptions", "IRR", "Pref Rate"), so the first pass filled one row of nine. Each row
  now has helper words (`_CHECKLIST_HINTS`) and an anchor phrase that must appear in the
  passage (`_ROW_ANCHORS`); without the anchor "Sale Price" took a list of nearby housing
  developments and "Pref Rate" a retail capital-market note out of a 160,000-character
  report. Measured on the reference report: the purchase and loan history, the road-cost
  agreement, the entitlement timing and the priority diligence list all reach the tab; the
  fee, preferred return and profit split rows do not, because the report never quotes the
  partnership agreement. Those are a document question, like the Legal fill.
- **The Proforma fill (`purpose="proforma"`, owner's yes, 2026-10-12)** uses the same
  reading engine (`document_evidence`) with its own plan: the partnership's own agreement
  before the general partner's operating agreement (the first pick took the GP's), executed
  amendments only, the NEWEST tax record and note (not tied to the deal's year), the loan
  modification, any recorded cost-share agreement and any budget. Sales are NOT left out
  here: on the reference property the road cost share is filed under the sale that created
  it. It needs a longer list per kind than the lookups use (`per_kind=12`): with four, the
  acquisition branch's two copies and the two newest amendments crowded out the partnership
  agreement itself. Nothing is modelled: no sale price, return or multiple is worked out.
  The Proforma tab's own reply also carries a one-line meaning for each row, because the
  first blind run put the road-cost agreement under "Costs to Complete" and a market price
  range under "Sale Price".
