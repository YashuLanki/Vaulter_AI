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
# Under output/, not the top level: this report is machine-made and can be
# rebuilt by re-running this script, which is exactly what output/ means. It
# sat at the top level until 2026-10-01, where it read as one of the handful of
# folders the team is meant to open.
OUT_DIRNAME = "output/duplicates"

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


def _confirmed_groups(buckets):
    """Every group of files PROVEN to be the same file, from both passes.

    Two passes find duplicates: verify_duplicates.py compares the copies that
    share a name and a size, and find_content_duplicates.py finds the same
    contents filed under different names. They overlap -- a file with three
    copies, two under one name and one under another, is found by both -- and
    listing it twice would put the same path in two groups with two different
    KEEP suggestions. So groups that share a path are merged here, and each
    path appears exactly once.

    Only groups where EVERY copy was compared byte for byte are included. A
    same-name group that turned out to hold different files, one that was never
    compared, and one only PARTLY compared (some copies cloud-only, so matched
    on name and size alone -- wrong about 5% of the time on this library) are
    all left out. The sheet this feeds is the one people delete from, and the
    reason given for it (2026-09-24) was that nobody wants to delete a file
    that only looked like a duplicate. The partial groups are counted in the
    README, with what it would take to bring them in.
    """
    verdicts, _ = _verdicts()
    content = _renamed_groups()

    # Every source group, as (paths, size, verdict text).
    sources = []
    for rec in buckets["document"]:
        v = verdicts.get("{}\t{}".format(rec["name"], rec["size"]), "not checked")
        if v.startswith("identical - all"):
            sources.append((rec["paths"], rec["size"], v))
    if content:
        for rec in content["groups"]:
            paths = rec.get("paths", [])
            if len(paths) > 1:
                sources.append((paths, rec.get("size", 0),
                                "identical - all {} copies compared".format(len(paths))))

    # Merge any two groups that share a path (union-find, keyed by path).
    parent = {}

    def find(p):
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    for paths, _size, _v in sources:
        for p in paths:
            parent.setdefault(p, p)
        first = find(paths[0])
        for p in paths[1:]:
            parent[find(p)] = first

    members = defaultdict(set)
    sizes = {}
    for paths, size, _v in sources:
        root = find(paths[0])
        members[root].update(paths)
        sizes[root] = size

    groups = []
    for root, paths in members.items():
        paths = sorted(paths)
        names = sorted({p.rsplit("/", 1)[-1] for p in paths})
        size = sizes[root]
        proof = "identical - all {} copies compared byte for byte".format(len(paths))
        groups.append({
            "paths": paths,
            "names": names,
            "size": size,
            "copies": len(paths),
            "wasted": size * (len(paths) - 1),
            "proof": proof,
            "keeper": _suggest_keeper(paths),
            "folders": sorted({_deal_folder(p) for p in paths}),
        })
    groups.sort(key=lambda g: (-g["wasted"], -g["copies"], g["names"][0]))
    return groups


def _verdict_summary():
    """One line for the README about the byte-for-byte pass."""
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
    """One line for the README. Says plainly when the pass has not run --
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
    note = "{:,} groups, {} -- in the workbook, marked 'different names, same contents'".format(
        len(groups), _human_bytes(waste))
    if data.get("stopped_early"):
        note += " (partial run -- hit its time limit)"
    if data.get("skipped_cloud_only"):
        note += "; {:,} candidates not checked (cloud-only, would need downloading)".format(
            data["skipped_cloud_only"])
    return note


# --- Spreadsheet -------------------------------------------------------------

# Columns a PERSON fills in, never the script. Carried forward across re-runs.
CHECK_COLUMNS = ["Read and confirmed?", "Confirmed by", "Date confirmed", "Notes"]
CHECK_CHOICES = ["Yes - confirmed duplicate", "NO - not a duplicate", "Keep - copy of record"]
PATH_KEY = "Path in library"


def _link_base(out_dir, root):
    """How a link in the workbook reaches the library.

    RELATIVE when the workbook sits inside the library -- which it does on every
    machine whose team folder is in its proper place, since `Vaulter AI Shared`
    lives inside the library -- so the same link works on every teammate's
    computer, not only the one that built the report. Absolute otherwise.
    """
    from urllib.parse import quote
    try:
        rel = os.path.relpath(root, out_dir)
    except ValueError:          # different drives
        return None
    return quote(rel.replace(os.sep, "/"), safe="/")


def _library_link(base, root, rel_path):
    """A link Excel will actually open.

    Measured in real Excel (2026-09-24): a plain relative link is cut off at the
    first '#', and 1,355 of the confirmed-duplicate paths contain one ("DVD #1").
    Percent-encoding every reserved character fixes it -- the encoded link opened
    a file whose path held '#', '&', '%', ';', '+', commas, brackets and spaces.
    """
    from urllib.parse import quote
    if base is None:
        return "file:///" + quote(os.path.join(root, rel_path).replace(os.sep, "/"), safe="/:")
    return base + "/" + quote(rel_path, safe="/")


def _previous_checks(path):
    """What people have already written in the check columns of the workbook
    being replaced, keyed by the file's path in the library.

    The report is regenerated whole, and a person's own verification is the one
    thing in it that cannot be regenerated -- so it is read back out first and
    put back on the same rows. Keyed by the in-library path, which is the same
    on every machine, never by the full local path.
    """
    if not path.exists():
        return {}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = [str(h) if h is not None else "" for h in next(rows, [])]
    except Exception:
        return {}
    if PATH_KEY not in header or any(c not in header for c in CHECK_COLUMNS):
        return {}
    key_i = header.index(PATH_KEY)
    idx = [header.index(c) for c in CHECK_COLUMNS]
    kept = {}
    for row in rows:
        if key_i >= len(row) or not row[key_i]:
            continue
        vals = [row[i] if i < len(row) else None for i in idx]
        if any(v not in (None, "") for v in vals):
            kept[str(row[key_i])] = vals
    return kept


def _write_workbook(out_dir, buckets, meta):
    """One sheet, holding every confirmed duplicate and nothing else.

    Asked for directly (2026-09-24): the workbook is the list somebody works
    through to remove duplicates, so it holds only files proven to be copies --
    both the same file under the same name and the same file under different
    names -- and no sheet of things that are NOT duplicates. The disproved and
    unverified same-name groups, the program files and the front page are
    dropped from the workbook; the README beside it carries the caveats.

    Also asked for the same day: nothing is deleted until a PERSON has opened
    the copies and read them. So every row links to its file and its folder,
    and four columns are left for that person to record the check. Those
    columns survive a re-run.
    """
    import pandas as pd
    from openpyxl.worksheet.datavalidation import DataValidation

    path = out_dir / "duplicate_files.xlsx"
    previous = _previous_checks(path)
    base = _link_base(out_dir, meta["root"])

    rows, links = [], []
    for n, g in enumerate(_confirmed_groups(buckets), 1):
        renamed = len(g["names"]) > 1
        spread = "across different deals" if len(g["folders"]) > 1 else "same deal folder"
        for p in g["paths"]:
            folder = p.rsplit("/", 1)[0] if "/" in p else ""
            checks = previous.get(p, [None] * len(CHECK_COLUMNS))
            row = {
                "Group": n,
                "File name": p.rsplit("/", 1)[-1],
                "Open file": "open file",
                "Open folder": "open folder",
                "Suggestion": "KEEP (suggestion)" if p == g["keeper"] else "duplicate",
            }
            row.update(dict(zip(CHECK_COLUMNS, checks)))
            row.update({
                "Matched by": "different names, same contents" if renamed else "same name",
                "Other names for this same file": ", ".join(g["names"])[:300] if renamed else "",
                "Copies": g["copies"],
                "Size of each": _human_bytes(g["size"]),
                "Space wasted by this group": _human_bytes(g["wasted"]),
                "Contents actually compared?": g["proof"],
                "Copies sit": spread,
                "Folder it is in": folder or "(top of the library)",
                "Full path on this computer": os.path.join(meta["root"], p.replace("/", os.sep)),
                PATH_KEY: p,
                "Size in bytes": g["size"],
                "Wasted in bytes": g["wasted"],
            })
            rows.append(row)
            links.append((_library_link(base, meta["root"], p),
                          _library_link(base, meta["root"], folder) if folder else base or ""))

    columns = (["Group", "File name", "Open file", "Open folder", "Suggestion"] + CHECK_COLUMNS
               + ["Matched by", "Other names for this same file", "Copies", "Size of each",
                  "Space wasted by this group", "Contents actually compared?", "Copies sit",
                  "Folder it is in", "Full path on this computer", PATH_KEY,
                  "Size in bytes", "Wasted in bytes"])
    frame = pd.DataFrame(rows, columns=columns)

    with pd.ExcelWriter(path, engine="openpyxl") as xl:
        frame.to_excel(xl, sheet_name="Confirmed duplicates", index=False)
        ws = xl.book.worksheets[0]
        file_col = columns.index("Open file") + 1
        folder_col = columns.index("Open folder") + 1
        for r, (file_link, folder_link) in enumerate(links, 2):
            c = ws.cell(r, file_col); c.hyperlink = file_link; c.style = "Hyperlink"
            c = ws.cell(r, folder_col); c.hyperlink = folder_link; c.style = "Hyperlink"
        if rows:
            col = chr(ord("A") + columns.index(CHECK_COLUMNS[0]))
            dv = DataValidation(type="list", formula1='"{}"'.format(",".join(CHECK_CHOICES)),
                                allow_blank=True, showErrorMessage=False)
            ws.add_data_validation(dv)
            dv.add("{0}2:{0}{1}".format(col, len(rows) + 1))
        widths = {"A": 8, "B": 62, "C": 10, "D": 12, "E": 18, "F": 26, "G": 16, "H": 14,
                  "I": 40, "J": 30, "K": 60, "L": 8, "M": 12, "N": 16, "O": 44, "P": 22,
                  "Q": 95, "R": 115, "S": 95}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "C2"
    return path, len(rows), frame["Group"].nunique() if rows else 0, len(previous)


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
    verdicts, _ = _verdicts()
    heads = [verdicts.get("{}	{}".format(r["name"], r["size"]), "not checked").split(" -")[0]
             for r in docs]
    text = """# Duplicate files

Built {generated}.

**Nothing here has been deleted, moved or copied.** This folder holds a report
and nothing else. Every file it describes is still exactly where it was.

## What to open

- **duplicate_files.html** -- open this first. A readable page: the headline
  numbers, where the duplication is concentrated, and the biggest groups with
  every location listed under them.
- **duplicate_files.xlsx** -- the list to work from. ONE sheet, holding only
  files proven to be copies of each other: {conf_groups:,} groups, {conf_rows:,}
  rows, one row per copy. Every copy in every group was read in full and
  compared byte for byte -- nothing in this sheet was matched on name alone.
  Both kinds are there, told apart by the "Matched by" column -- the same file
  under the same name, and the same file filed under different names.

## How to work through it -- nothing is deleted until a person has read the copies

The computer's byte-for-byte comparison is the reason a file is on the list. It
is not the reason to delete it. That is a person's call, made by opening the
copies and reading them:

1. Click **open file** on each row of a group to read that copy. Click **open
   folder** to see where it sits, and to delete it from there if you decide to.
2. In **Read and confirmed?**, pick from the dropdown: "Yes - confirmed
   duplicate", "NO - not a duplicate", or "Keep - copy of record". Put your name
   in **Confirmed by** and the date in **Date confirmed**. **Notes** is yours.
3. Delete a copy only after its row says "Yes - confirmed duplicate" in your
   own hand, and one copy in the group is marked "Keep".

What you write in those four columns is kept when this report is re-run --
they are read out of the old workbook and put back on the same rows. Close the
workbook before re-running, or the re-run cannot write it and stops.

The links are relative to where the workbook sits in the library, so they work
on every teammate's computer that has the team folder in its usual place.

## What is deliberately NOT in the workbook

- **Same-name, same-size pairs whose contents turned out to be DIFFERENT** --
  {disproved:,} groups. Their names match and their sizes match and they are
  not the same file. They are left out so that nobody deletes one.
- **Same-name groups only PARTLY compared** -- {partial:,} groups. The copies on
  this computer matched, but one or more copies live only in the cloud and were
  never read, so those match on name and size alone. Left out rather than
  listed half-proven. To bring them in, download those copies (open the folder
  in OneDrive and mark it "Always keep on this device"), then re-run
  `python system/scripts/verify_duplicates.py` and this script.
- **Same-name pairs that could not be compared at all** -- {unverified:,} groups
  whose copies are all cloud-only. No opinion is offered either way; they may
  or may not be duplicates.
- **Program files** and **emails** (see below).

## How it was checked

- Contents compared: {verdict_summary}
- Same file, different name: {renamed_summary}

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
  they sit in their own section of the web page, not in the workbook.

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
        conf_groups=meta.get("conf_groups", 0), conf_rows=meta.get("conf_rows", 0),
        disproved=heads.count("NOT IDENTICAL"), unverified=heads.count("not checked"),
        partial=heads.count("identical so far"),
        verdict_summary=_verdict_summary(), renamed_summary=_renamed_summary(),
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
    book, conf_rows, conf_groups, carried = _write_workbook(out_dir, buckets, meta)
    meta["conf_rows"], meta["conf_groups"] = conf_rows, conf_groups
    readme = _write_readme(out_dir, buckets, meta)

    docs = buckets["document"]
    print()
    print("  {:,} groups of duplicated documents".format(len(docs)))
    print("  {:,} extra copies".format(sum(r["copies"] - 1 for r in docs)))
    print("  {} of space they take up".format(_human_bytes(sum(r["wasted"] for r in docs))))
    print("  {:,} groups confirmed as duplicates in the workbook ({:,} rows)".format(
        conf_groups, conf_rows))
    print("  {:,} rows already checked by a person, carried forward from the last run".format(carried))
    print("  {:,} groups of program files (web page only)".format(len(buckets["program"])))
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
