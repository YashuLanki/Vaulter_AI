"""
find_duplicates.py
------------------
Find files that exist more than once in the firm's document library, and write
a report a person can actually read into the team folder.

    python system/scripts/find_duplicates.py

WHAT IT COSTS: nothing. It reads the local file list (the same list
`index-corpus` builds), which holds names, sizes and dates only. It opens no
documents and downloads nothing out of OneDrive -- which matters, because the
library is hundreds of thousands of files that live in the cloud until
something asks for them. Reading them all to compare contents would pull
hundreds of gigabytes down onto this machine.

WHAT "DUPLICATE" MEANS HERE: same filename AND same exact size, in two or more
places. That is a strong signal and it is free. It is not proof -- two genuinely
different files can coincide -- so the report says so on its own front page and
never tells anyone to delete anything.

WHAT IT DELIBERATELY LEAVES OUT: archived Outlook emails (.msg). This system
does not make archived email readable to its users, and an email's filename is
usually its subject line. The report says out loud that they were excluded, so
their absence is never read as "there aren't any".

NOTHING IS DELETED, MOVED OR COPIED. The report is the whole output.
"""

import html
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

INDEX_DB = PROJECT_ROOT / "data" / "corpus_index.db"
OUT_DIRNAME = "duplicates"

# Archived Outlook email. Excluded by decision, not by oversight -- see the
# module docstring. Counted so the report can say how much was set aside.
EXCLUDED_SUFFIXES = {".msg"}

# Files a program made for itself, not documents anyone wrote. They cost almost
# none of the wasted space, and a big pile of copies usually means a whole folder
# got duplicated, so they get their own section rather than competing with real
# documents for the top of the list.
PROGRAM_SUFFIXES = {
    ".bak", ".tmp", ".temp", ".log", ".ini", ".cfg", ".lock", ".dwl", ".dwl2",
    ".idx", ".ds_store", ".swp", ".err", ".chk",
}
PROGRAM_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "ehthumbs.db"}

# A file this small is a setting or a marker, never a document someone wrote.
# The library's single most-duplicated file is 5 bytes and exists 186 times.
PROGRAM_MAX_BYTES = 1024

# How many groups get full detail in the web page. The spreadsheet carries
# every one; a page listing 75,000 groups is not a page anyone opens.
HTML_GROUP_LIMIT = 250

# Paths shown per group in the web page before it says "and N more".
HTML_PATHS_PER_GROUP = 12


# --- Reading the file list ---------------------------------------------------

def _index_age():
    """How old the file list is, and where it points.

    Stated on the report's own front page. This project has been burned by a
    confident answer built on a stale list -- a freshness claim is only ever as
    fresh as the thing it read.
    """
    con = sqlite3.connect("file:{}?mode=ro".format(INDEX_DB), uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        meta = {}
    finally:
        con.close()

    built_raw = meta.get("built_at", "")
    age_days = None
    try:
        built = datetime.fromisoformat(built_raw)
        if built.tzinfo is None:
            built = built.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(timezone.utc) - built).total_seconds() / 86400
    except ValueError:
        pass
    return built_raw, age_days, meta.get("root", ""), meta.get("file_count", "")


def _duplicate_groups():
    """Every (name, size) that appears more than once, with all its paths.

    The stored paths are RELATIVE to the library root -- not full paths. Every
    place that treats one as a full path silently gets the wrong answer rather
    than an error, so the root travels with them from here on.
    """
    con = sqlite3.connect("file:{}?mode=ro".format(INDEX_DB), uri=True)
    con.execute("PRAGMA temp_store = MEMORY")
    rows = con.execute(
        """
        SELECT f.name, f.size, f.path
          FROM files f
          JOIN (SELECT name, size FROM files
                 WHERE size > 0
                 GROUP BY name, size
                HAVING COUNT(*) > 1) d
            ON f.name = d.name AND f.size = d.size
         WHERE f.size > 0
        """
    )
    groups = defaultdict(list)
    for name, size, path in rows:
        groups[(name, size)].append(path.replace("\\", "/"))
    con.close()
    return groups


# --- Classifying -------------------------------------------------------------

def _classify(name, size):
    """'excluded' (email), 'program' (machine-made), or 'document'."""
    suffix = os.path.splitext(name)[1].lower()
    if suffix in EXCLUDED_SUFFIXES:
        return "excluded"
    if suffix in PROGRAM_SUFFIXES or name.lower() in PROGRAM_NAMES:
        return "program"
    if size < PROGRAM_MAX_BYTES:
        return "program"
    return "document"


def _suggest_keeper(paths):
    """Which copy looks most likely to be the real one.

    A SUGGESTION, never an instruction -- the report labels it that way
    everywhere it appears. The rule is deliberately dull and explainable:
    the copy sitting in the shallowest folder, then the shortest path, then
    alphabetical so the same file always gets the same answer. A file buried
    fifteen folders deep in someone's working copy is less likely to be the
    one of record than the same file near the top of a deal folder.
    """
    return min(paths, key=lambda p: (p.count("/"), len(p), p))


def _top_folder(path):
    """The first folder below the library root -- usually the region or deal."""
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else "(loose at the top of the library)"


def _deal_folder(path):
    """A folder deep enough to name the actual deal.

    The library's top level is a region ("!PROPERTIES/ARIZONA/..."), so the top
    folder alone groups half the library under one heading. Two levels in is
    where a deal is usually named; anything shallower is reported as it is.
    """
    parts = path.split("/")
    if len(parts) > 3:
        return "/".join(parts[:3])
    return _top_folder(path)


def _human_bytes(n):
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return "{:,.0f} {}".format(n, unit) if unit == "bytes" else "{:,.1f} {}".format(n, unit)
        n /= 1024.0


def _build():
    """Turn the raw groups into the three buckets the report is built from."""
    buckets = {"document": [], "program": [], "excluded": []}

    for (name, size), paths in _duplicate_groups().items():
        kind = _classify(name, size)
        paths = sorted(paths)
        copies = len(paths)
        buckets[kind].append({
            "name": name,
            "size": size,
            "copies": copies,
            "wasted": size * (copies - 1),
            "paths": paths,
            "keeper": _suggest_keeper(paths),
            "folders": sorted({_deal_folder(p) for p in paths}),
        })

    for items in buckets.values():
        items.sort(key=lambda r: (-r["wasted"], -r["copies"], r["name"]))
    return buckets


# --- Same file under a different name ----------------------------------------

CONTENT_JSON = PROJECT_ROOT / "data" / "content_duplicates.json"
VERDICT_JSON = PROJECT_ROOT / "data" / "verified_duplicates.json"


def _verdicts():
    """Per-group results from verify_duplicates.py, keyed "name	size".

    Missing file means that pass has not been run. Every row then reads
    "not checked", never a blank that a reader would take for approval.
    """
    try:
        data = json.loads(VERDICT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, None
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), dict):
        return {}, None
    return data["verdicts"], data


def _renamed_groups():
    """Findings from find_content_duplicates.py, if it has been run.

    Kept in a separate file, and a separate script, because proving two
    differently-named files are the same means reading every byte of both --
    minutes of work, against the seconds this script takes. Absent file means
    that pass has not been run, which the report says rather than implying
    there is nothing to find.
    """
    try:
        data = json.loads(CONTENT_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return None
    return data


def _renamed_frame(pd, meta):
    data = _renamed_groups()
    if not data or not data["groups"]:
        return None
    rows = []
    for n, rec in enumerate(data["groups"], 1):
        paths = rec.get("paths", [])
        keeper = _suggest_keeper(paths) if paths else None
        for p in paths:
            rows.append({
                "Group": n,
                "File name": p.rsplit("/", 1)[-1],
                "Other names for this same file": ", ".join(rec.get("distinct_names", []))[:300],
                "Copies": rec.get("copies"),
                "Size of each": _human_bytes(rec.get("size", 0)),
                "Space wasted by this group": _human_bytes(rec.get("wasted", 0)),
                "Suggestion": "KEEP (suggestion)" if p == keeper else "duplicate",
                "Where this copy is": p,
                "Full path on this computer": os.path.join(meta["root"], p.replace("/", os.sep)),
                "Proof": "contents compared byte for byte",
                "Wasted in bytes": rec.get("wasted", 0),
            })
    return pd.DataFrame(rows)


def _verdict_summary():
    """One line for the front sheet about the byte-for-byte pass."""
    verdicts, meta = _verdicts()
    if not verdicts:
        return ("NOT RUN. Matching is on name + exact size, which is a strong signal "
                "but NOT proof. Run: python system/scripts/verify_duplicates.py")
    import collections as _c
    tally = _c.Counter(v.split(" -")[0] for v in verdicts.values())
    compared = tally["identical"] + tally["identical so far"] + tally["NOT IDENTICAL"]
    parts = ["{:,} groups checked".format(len(verdicts))]
    if compared:
        parts.append("{:,} confirmed identical".format(tally["identical"] + tally["identical so far"]))
        parts.append("{:,} found NOT identical ({:.1f}% of those compared)".format(
            tally["NOT IDENTICAL"], 100.0 * tally["NOT IDENTICAL"] / compared))
    if tally["not checked"]:
        parts.append("{:,} could not be checked (cloud-only)".format(tally["not checked"]))
    if meta and meta.get("stopped_early"):
        parts.append("PARTIAL RUN - hit its time limit")
    return "; ".join(parts)


def _renamed_summary():
    """One line for the front sheet. Says plainly when the pass has not run --
    a silent absence would read as 'there are none', which is the confident
    empty answer this whole report is careful to avoid."""
    data = _renamed_groups()
    if data is None:
        return ("NOT CHECKED. Run: python system/scripts/find_content_duplicates.py "
                "-- it compares contents, which finds copies somebody renamed.")
    groups = data.get("groups", [])
    if not groups:
        return "Checked, none found among the files readable without downloading."
    waste = sum(g.get("wasted", 0) for g in groups)
    note = "{:,} groups, {} -- see the 'Same file different name' sheet".format(
        len(groups), _human_bytes(waste))
    if data.get("stopped_early"):
        note += " (partial run -- hit its time limit)"
    if data.get("skipped_cloud_only"):
        note += "; {:,} candidates not checked (cloud-only, would need downloading)".format(
            data["skipped_cloud_only"])
    return note


# --- Spreadsheet -------------------------------------------------------------

def _write_workbook(out_dir, buckets, meta):
    import pandas as pd

    path = out_dir / "duplicate_files.xlsx"

    verdicts, _vmeta = _verdicts()

    def sheet_frame(items):
        rows = []
        for n, rec in enumerate(items, 1):
            verdict = verdicts.get(
                "{}	{}".format(rec["name"], rec["size"]),
                "not checked - run verify_duplicates.py")
            spread = "across different deals" if len(rec["folders"]) > 1 else "same deal folder"
            for p in rec["paths"]:
                rows.append({
                    "Group": n,
                    "File name": rec["name"],
                    "Copies": rec["copies"],
                    "Size of each": _human_bytes(rec["size"]),
                    "Space wasted by this group": _human_bytes(rec["wasted"]),
                    "Contents actually compared?": verdict,
                    "Suggestion": "KEEP (suggestion)" if p == rec["keeper"] else "duplicate",
                    "Folder it is in": p.rsplit("/", 1)[0] if "/" in p else "(top of the library)",
                    "Full path on this computer": os.path.join(meta["root"], p.replace("/", os.sep)),
                    "Copies sit": spread,
                    "Size in bytes": rec["size"],
                    "Wasted in bytes": rec["wasted"],
                })
        return pd.DataFrame(rows)

    doc_waste = sum(r["wasted"] for r in buckets["document"])
    front = pd.DataFrame([
        ("What this is", "Files that appear more than once in the firm's document library."),
        ("Built on", meta["generated"]),
        ("Read from", "the local list of file names -- no document was opened or downloaded"),
        ("File list last rebuilt", meta["age_text"]),
        ("Files in the library", meta["file_count"]),
        ("", ""),
        ("Real documents duplicated", "{:,} groups".format(len(buckets["document"]))),
        ("  extra copies of them", "{:,}".format(sum(r["copies"] - 1 for r in buckets["document"]))),
        ("  space those copies take", _human_bytes(doc_waste)),
        ("", ""),
        ("Program files duplicated", "{:,} groups, {}".format(
            len(buckets["program"]), _human_bytes(sum(r["wasted"] for r in buckets["program"])))),
        ("Emails left out on purpose", "{:,} groups, {}".format(
            len(buckets["excluded"]), _human_bytes(sum(r["wasted"] for r in buckets["excluded"])))),
        ("", ""),
        ("How two files were matched", "Same file name AND same exact size."),
        ("Is that proof?", "No. It is a strong signal. Two different files can coincide. "
                           "Check before deleting anything."),
        ("Has anything been deleted?", "No. Nothing was deleted, moved or copied."),
        ("The KEEP column", "A SUGGESTION -- the copy in the shallowest folder. Not an instruction."),
        ("", ""),
        ("Same file, different name", _renamed_summary()),
        ("Contents actually compared?", _verdict_summary()),
        ("", ""),
        ("SHEET: Confirmed duplicates", "Contents compared and they MATCH. The only sheet safe to act on."),
        ("SHEET: NOT duplicates", "Same name, same size, but DIFFERENT contents. Do NOT delete these."),
        ("SHEET: Unverified", "Not compared -- copies are cloud-only. No opinion offered either way."),
    ], columns=["", " "])

    # Split by what the byte-for-byte check actually proved, so the sheet
    # somebody works from contains only real duplicates. The disproved ones are
    # MOVED, never dropped -- "same name, same size, different document" is the
    # most useful thing in this whole report and deleting it would throw away
    # the warning it exists to give.
    def _verdict_of(rec):
        return verdicts.get("{}	{}".format(rec["name"], rec["size"]), "not checked")

    confirmed, disproved, unverified = [], [], []
    for rec in buckets["document"]:
        v = _verdict_of(rec)
        (disproved if v.startswith("NOT IDENTICAL")
         else confirmed if v.startswith("identical")
         else unverified).append(rec)

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        front.to_excel(xl, sheet_name="Read me first", index=False)
        sheet_frame(confirmed).to_excel(xl, sheet_name="Confirmed duplicates", index=False)
        sheet_frame(disproved).to_excel(xl, sheet_name="NOT duplicates-do not delete", index=False)
        sheet_frame(unverified).to_excel(xl, sheet_name="Unverified-not checked", index=False)
        sheet_frame(buckets["program"]).to_excel(xl, sheet_name="Program files", index=False)

        renamed = _renamed_frame(pd, meta)
        if renamed is not None:
            renamed.to_excel(xl, sheet_name="Same file different name", index=False)

        widths = {"A": 46, "B": 62, "C": 10, "D": 16, "E": 26, "F": 20,
                  "G": 95, "H": 115, "I": 22}
        for sheet in xl.book.worksheets:
            for col, w in widths.items():
                sheet.column_dimensions[col].width = w
            sheet.freeze_panes = "A2"
    return path


# --- Web page ----------------------------------------------------------------

_CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#1c1b19;--dim:#6b6a67;--line:#e3e2de;
--warn:#8a5a00;--warnbg:#fff8e6;--accent:#2f5d50}
*{box-sizing:border-box}
body{margin:0;padding:32px 16px 72px;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:28px;margin:0 0 4px;letter-spacing:-.3px}
h2{font-size:19px;margin:36px 0 10px}
.sub{color:var(--dim);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:0 0 16px}
.note{background:var(--warnbg);border-color:#f0e2bd;color:var(--warn)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:0 0 20px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.tile .n{font-size:26px;font-weight:600;letter-spacing:-.5px}
.tile .l{color:var(--dim);font-size:13px;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
td.num{text-align:right;white-space:nowrap}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 16px;margin:0 0 8px}
summary{cursor:pointer;font-weight:600}
summary .meta{font-weight:400;color:var(--dim);margin-left:8px;font-size:13px}
.path{font:12.5px/1.5 ui-monospace,Consolas,monospace;color:#333;padding:3px 0;
word-break:break-all;border-bottom:1px dotted var(--line)}
.keep{color:var(--accent);font-weight:700}
.flag{display:inline-block;background:#fff1f0;color:#9b2c22;border-radius:4px;
padding:1px 7px;font-size:12px;margin-left:6px;font-weight:400}
footer{color:var(--dim);font-size:13px;margin-top:40px}
code{background:#eeeeeb;padding:1px 5px;border-radius:4px;font-size:13px}
"""


def _write_page(out_dir, buckets, meta):
    e = html.escape
    docs, prog, excl = buckets["document"], buckets["program"], buckets["excluded"]
    doc_waste = sum(r["wasted"] for r in docs)
    doc_extra = sum(r["copies"] - 1 for r in docs)

    p = ["<!doctype html><html><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width,initial-scale=1'>",
         "<title>Duplicate files</title><style>", _CSS, "</style></head><body><div class='wrap'>"]

    p.append("<h1>Duplicate files in the firm's document library</h1>")
    p.append("<p class='sub'>Built {} &middot; nothing was deleted, moved or copied</p>".format(
        e(meta["generated"])))

    p.append("<div class='tiles'>")
    for n, l in [("{:,}".format(len(docs)), "groups of duplicated documents"),
                 ("{:,}".format(doc_extra), "extra copies of them"),
                 (_human_bytes(doc_waste), "space those extra copies take"),
                 (meta["file_count"], "files in the library")]:
        p.append("<div class='tile'><div class='n'>{}</div><div class='l'>{}</div></div>".format(
            e(str(n)), e(l)))
    p.append("</div>")

    p.append("<div class='card note'><strong>Before you delete anything, read this.</strong><br>"
             "Two files are counted as the same here when they have the <strong>same name and the "
             "exact same size</strong>. That is a strong signal, and it was free to work out &mdash; "
             "no document was opened and nothing was downloaded out of OneDrive. It is "
             "<strong>not proof</strong>. Two genuinely different files can happen to match, which "
             "is likeliest when the copies sit under different deals &mdash; those are flagged below."
             "<br><br>The <span class='keep'>KEEP</span> mark is a <strong>suggestion</strong>: it "
             "picks the copy in the shallowest folder, on the reasoning that a file buried deep in "
             "someone's working copy is less likely to be the one of record. A person decides, not "
             "this report.<br><br>Paths below are shown from inside the library, which on "
             "this computer is <code>{}</code>. The spreadsheet carries the full path for "
             "each copy.</div>".format(e(meta["root"])))

    p.append("<div class='card'><strong>What was left out, on purpose.</strong><br>"
             "<strong>Archived Outlook emails</strong> &mdash; {:,} groups, {}. This system does "
             "not make archived email readable to its users, and an email's file name is usually "
             "its subject line. They are counted here so their absence is never read as "
             "&ldquo;there aren't any&rdquo;.<br><br>"
             "<strong>Program files</strong> &mdash; {:,} groups, {}. Files a program made for "
             "itself rather than anyone writing them: settings, autosaves, drawing backups. They cost "
             "almost none of the wasted space, and a big pile of copies here usually means a whole "
             "folder got duplicated rather than a document worth chasing, so they sit in their own "
             "section at the bottom.<br><br>"
             "<strong>The list of file names this read</strong> was last rebuilt {}. Anything added "
             "to the library since then will not appear here.</div>".format(
                 len(excl), _human_bytes(sum(r["wasted"] for r in excl)),
                 len(prog), _human_bytes(sum(r["wasted"] for r in prog)),
                 e(meta["age_text"])))

    by_folder = defaultdict(lambda: [0, 0.0])
    for rec in docs:
        for f in rec["folders"]:
            by_folder[f][0] += 1
            by_folder[f][1] += rec["wasted"] / len(rec["folders"])
    top_folders = sorted(by_folder.items(), key=lambda kv: -kv[1][1])[:15]

    p.append("<h2>Where it is concentrated</h2>")
    p.append("<div class='card'><table><tr><th>Folder</th><th>Groups</th>"
             "<th>Space wasted</th></tr>")
    for name, (cnt, waste) in top_folders:
        p.append("<tr><td>{}</td><td class='num'>{:,}</td><td class='num'>{}</td></tr>".format(
            e(name), cnt, _human_bytes(waste)))
    p.append("</table></div>")

    shown = docs[:HTML_GROUP_LIMIT]
    p.append("<h2>Duplicated documents &mdash; biggest space saving first</h2>")
    p.append("<p class='sub'>Showing the top {:,} of {:,}. The spreadsheet beside this page has "
             "every one.</p>".format(len(shown), len(docs)))

    for rec in shown:
        flag = ("<span class='flag'>copies sit under different deals &mdash; check these really "
                "are the same file</span>") if len(rec["folders"]) > 1 else ""
        p.append("<details><summary>{}{}<span class='meta'>&mdash; {} copies, {} each, {} "
                 "wasted</span></summary>".format(
                     e(rec["name"]), flag, rec["copies"],
                     _human_bytes(rec["size"]), _human_bytes(rec["wasted"])))
        for path in rec["paths"][:HTML_PATHS_PER_GROUP]:
            mark = ("<span class='keep'>KEEP&nbsp;</span>" if path == rec["keeper"]
                    else "&nbsp;" * 7)
            p.append("<div class='path'>{}{}</div>".format(mark, e(path)))
        if rec["copies"] > HTML_PATHS_PER_GROUP:
            p.append("<div class='path'>&hellip; and {:,} more, listed in the spreadsheet</div>".format(
                rec["copies"] - HTML_PATHS_PER_GROUP))
        p.append("</details>")

    p.append("<h2>Program files (not documents)</h2>")
    p.append("<p class='sub'>Settings and backup files software made for itself. Listed for "
             "completeness &mdash; a lot of copies here usually means a whole folder got "
             "duplicated.</p>")
    p.append("<div class='card'><table><tr><th>File name</th><th>Copies</th>"
             "<th>Size each</th><th>Wasted</th></tr>")
    for rec in prog[:40]:
        p.append("<tr><td>{}</td><td class='num'>{:,}</td><td class='num'>{}</td>"
                 "<td class='num'>{}</td></tr>".format(
                     e(rec["name"]), rec["copies"], _human_bytes(rec["size"]),
                     _human_bytes(rec["wasted"])))
    p.append("</table></div>")

    p.append("<footer>Rebuild this any time with "
             "<code>python system/scripts/find_duplicates.py</code>. It reads the local list of "
             "file names only &mdash; it opens no documents and downloads nothing.</footer>")
    p.append("</div></body></html>")

    out = out_dir / "duplicate_files.html"
    out.write_text("".join(p), encoding="utf-8")
    return out


# --- Read me -----------------------------------------------------------------

def _write_readme(out_dir, buckets, meta):
    docs, prog, excl = buckets["document"], buckets["program"], buckets["excluded"]
    text = """# Duplicate files

Built {generated}.

**Nothing here has been deleted, moved or copied.** This folder holds a report
and nothing else. Every file it describes is still exactly where it was.

## What to open

- **duplicate_files.html** -- open this first. A readable page: the headline
  numbers, where the duplication is concentrated, and the biggest groups with
  every location listed under them.
- **duplicate_files.xlsx** -- the complete record. One row per copy, so you can
  sort and filter it yourself. The first sheet explains the columns.

## What counts as a duplicate

Two files with the **same name and the exact same size**, in two or more places.

That was free to work out -- it reads the list of file names this system already
keeps, opens no documents, and downloads nothing out of OneDrive. Comparing the
actual contents of every file would mean pulling hundreds of gigabytes onto one
computer.

It is a strong signal, **not proof**. Two genuinely different files can happen to
match. The report flags the riskiest case: copies sitting under different deals.

## The "KEEP" mark is a suggestion

It picks the copy in the shallowest folder, on the reasoning that a file buried
deep in someone's working copy is less likely to be the one of record. A person
decides what to keep. This report does not.

## What was left out, and why

- **Archived Outlook emails (.msg)** -- {excl_n:,} groups, {excl_waste}.
  This system does not make archived email readable to its users, and an email's
  file name is usually its subject line. Recorded here so their absence is never
  read as "there aren't any".
- **Program files** -- {prog_n:,} groups, {prog_waste}. Files a program made for
  itself rather than anyone writing them: settings, autosaves, drawing backups.
  They cost almost none of the wasted space, and a big pile of copies usually
  means a whole folder got duplicated rather than a document worth chasing, so
  they sit in their own section.

## How fresh this is

The list of file names it read was last rebuilt {age}. Anything added to the
library since then does not appear. To refresh it, run
`python system/main.py index-corpus`, then re-run this.

## Re-running it

    python system/scripts/find_duplicates.py

Takes seconds and overwrites this folder. Safe to run as often as you like.
""".format(
        generated=meta["generated"],
        excl_n=len(excl), excl_waste=_human_bytes(sum(r["wasted"] for r in excl)),
        prog_n=len(prog), prog_waste=_human_bytes(sum(r["wasted"] for r in prog)),
        age=meta["age_text"],
    )
    out = out_dir / "README.md"
    out.write_text(text, encoding="utf-8")
    return out


# --- Entry point -------------------------------------------------------------

def main():
    if not INDEX_DB.exists():
        print("There is no list of file names on this machine yet.")
        print("Build one first:  python system/main.py index-corpus")
        return 1

    built_at, age_days, root, file_count = _index_age()
    if age_days is None:
        age_text = "at an unknown time -- treat these results as possibly out of date"
    elif age_days < 1:
        age_text = "today ({} UTC)".format(built_at[:16].replace("T", " "))
    else:
        age_text = "{:.0f} day{} ago ({})".format(
            age_days, "s" if age_days >= 2 else "", built_at[:10])

    print("Reading the file list ({} files, rebuilt {})...".format(file_count, age_text))
    buckets = _build()

    meta = {
        "generated": datetime.now().strftime("%d %B %Y at %H:%M"),
        "age_text": age_text,
        "root": root,
        "file_count": "{:,}".format(int(file_count)) if str(file_count).isdigit() else str(file_count),
    }

    out_dir = Path(config.SHARED_DIR) / OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    page = _write_page(out_dir, buckets, meta)
    book = _write_workbook(out_dir, buckets, meta)
    readme = _write_readme(out_dir, buckets, meta)

    docs = buckets["document"]
    print()
    print("  {:,} groups of duplicated documents".format(len(docs)))
    print("  {:,} extra copies".format(sum(r["copies"] - 1 for r in docs)))
    print("  {} of space they take up".format(_human_bytes(sum(r["wasted"] for r in docs))))
    print("  {:,} groups of program files (their own section)".format(len(buckets["program"])))
    print("  {:,} groups of emails left out on purpose".format(len(buckets["excluded"])))
    print()
    print("Written to:")
    for f in (page, book, readme):
        print("  {}".format(f))
    print()
    print("Nothing was deleted, moved or copied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
