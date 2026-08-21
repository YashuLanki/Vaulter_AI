"""
scripts/check_answers.py
------------------------
Does this system's SHARED KNOWLEDGE hold up? Not "does the code run" -- the
other two suites cover that -- but "are the answers people actually receive
grounded in documents that exist, and does the system admit what it doesn't
know?"

    python system/scripts/check_answers.py

Why this exists
---------------
`check_screener.py` (106 checks) and `check_portfolio_comparison.py` (58) test
deterministic Python. Both pass while an answer to a person is still wrong,
because the wrongness lives in the knowledge the answer was built from, not in
the arithmetic. Measured 2026-08-11: Claude stated as fact that no documents
newer than 2026-08-03 existed for a property. There were 57. Every test passed;
the code was flawless; the answer was false. Nothing in this repo could have
caught it.

What this checks, and why each one is the difference between a right answer and
a confident wrong one:

  1. CITED DOCUMENTS EXIST.       A claim whose source is not a real file is a
                                  fabricated citation, which is worse than no
                                  citation -- it survives scrutiny.
  2. EVERY SUMMARY CAN BE DATED.  Without a source date, nothing can ever tell
                                  whether a summary is current, so "it doesn't
                                  say" is indistinguishable from "it's fine".
  3. EVERY SUMMARY DECLARES ITS GAPS.  The system's entire discipline is
                                  refusing to imply completeness it doesn't
                                  have. A summary with no Gaps section reads as
                                  exhaustive whether or not it is.
  4. FINDINGS CARRY CITATIONS.    Reported as a proportion, not pass/fail: prose
                                  legitimately contains connective sentences.
                                  A falling number is the signal.

It then writes a QUESTION SET for the part no script can do alone -- checking
whether Claude's actual answers are right. Those questions are derived from
the summaries at run time rather than stored, so no real firm fact ever lands
in this public repo, and the set can never go stale against the summaries.
Run it with the `answer-eval` skill.

Reads file NAMES only, out of the local index. Opens no documents, downloads
nothing, and makes no network or model calls.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, condition, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def note(text: str) -> None:
    """A measured number worth watching that is not itself pass/fail."""
    print(f"  ....  {text}")


def skip(name: str, why: str) -> None:
    print(f"  SKIP  {name}  — {why}")


# A path inside a backtick, ending in a readable document extension. This is
# how the summaries actually cite sources -- confirmed against all 49, which
# carry 762 of them. Windows and POSIX separators both appear.
_CITED_PATH = re.compile(r"`([^`\n]+?\.(?:pdf|docx?|xlsx?|xls|csv|txt|msg))`", re.I)

# "-- <file>.pdf, p.12" and its dash variants. 676 across the summaries.
_CITED_PAGE = re.compile(
    r"[—\-]{1,2}\s*([^\n,;`]+?\.(?:pdf|docx?|xlsx?|xls|txt))\s*,?\s*p+\.?\s*(\d+)", re.I)

_STAMP = re.compile(r"Source files as of:?\*{0,2}\s*(\d{4}-\d{2}-\d{2})")

# What proportion of substantive bullets carried a source when this check was
# first run (2026-08-14). MEASURED, not chosen --
# the first version of this file asserted 60% with nothing behind it, which is
# the exact habit this project removes everywhere else. It exists to catch the
# number FALLING, not to claim it is where it should be. Raise it only after a
# deliberate pass that improves coverage, never to make a run go green.
_CITED_BASELINE = 52

# Citations naming a document nobody can find. Lowered from 25 to 12 on
# 2026-08-20 after a correction pass, which is the only direction this number
# may ever move.
#
# HOW IT IS COUNTED also changed that day, so this figure and anything printed
# before it are not the same measurement. Two kinds of false failure were
# removed: a real filename containing two consecutive spaces (the drive is full
# of them) counted as missing, and a citation naming one of this system's OWN
# output files counted as missing because the firm's library will never hold it.
# The same write-ups reported 41 under the old count and 26 under the corrected
# one, with no change to the data at all.
#
# The correction pass then fixed 14 mentions across 7 write-ups. Worth knowing
# what they turned out to be, because almost none was a fabricated source:
# a one-letter typo in a consultant's name; two consecutive spaces; a property
# name dropped off the front of a filename; a filename written in the house
# YYMMDD style when the real file was named differently; a range written as
# "Sht1.PDF through Sht5.PDF", where the tail is shorthand and not a filename at
# all; and "Budget A/B.pdf", meaning two files, where the slash read as a folder
# separator. Two of the corrected citations also carried a FOLDER that did not
# hold the file -- checked against the drive and replaced with the real one,
# since a wrong path sends a reader somewhere that does not exist.
#
# The 12 that remain are three distinct files, all deliberately left: an
# executed June 2026 letter of intent for antelope-ellis that is not on the
# drive under any similar name (the only "fully executed" files for that
# property are leases from 2011 and 2021), and two WCR 34 settlement statements
# whose folders hold dozens of closing documents but nothing matching. Several
# generically-named settlement statements exist and choosing one would be a
# guess. Fix them by finding the real document, never by picking the nearest
# name.
_UNRESOLVED_BASELINE = 12

# Summaries whose "Source files as of" is written as prose rather than a date.
# Was 10 when this check was written; 5 after the active-stage ones were fixed on
# 2026-08-21; now ZERO -- all 49 carry a machine-readable date, so every summary
# in the library can be currency-checked and any new prose stamp fails at once.
#
# Worth knowing what the fix actually was, because it was not a dating exercise:
# every one of those summaries ALREADY stated its newest source date. It was
# written as prose -- "newest file read/checked was <a named monthly report>
# (prepared 12/15/2025)", "1/13/2026 (file save date on newest document
# reviewed)" -- so a person could read it and this code could not. The date each
# one already named was moved to the front in YYYY-MM-DD and the prose kept.
#
# Nothing was newly dated, and nothing was stamped with today. Stamping a
# weeks-old summary with today's date would make a stale summary look current,
# which is the exact opposite of what the stamp is for -- it would silence the
# warning rather than answer it.
#
# One judgement worth recording: where a summary named BOTH a filesystem
# timestamp and an older date from the document's own filename, the stamp is the
# TIMESTAMP. That is what a currency check compares against, so the older date
# would flag every re-synced file as newer than the summary and cry wolf. The
# prose still explains the discrepancy, which is where that belongs.
_UNREADABLE_STAMP_BASELINE = 0


# Files this system produces itself, which are cited in summaries as the source
# of a fact and are perfectly real -- they are simply not documents from the
# firm's library, so the library's file list will never contain them. Counting
# them as unfindable blames the summary for citing its actual source.
_OWN_FILES = {
    "vaulter_project_master.csv",
    "property_coordinates.csv",
    "builtin_properties.json",
    "portfolio_comparison_index.json",
    "cost_assumptions.json",
}


def _comparable(name: str) -> str:
    """
    A filename reduced to what a person would recognise it by.

    Real filenames on the drive contain runs of two and three spaces, usually
    where a name was assembled by hand. Anyone citing one collapses those
    without noticing, and so does most software that touches the text on the
    way. Comparing raw strings therefore reports a file as missing when it is
    sitting right there under a name that differs by one invisible character --
    the checker being wrong about the thing it is checking, which is worth
    more caution here than a missed citation.
    """
    return " ".join(name.lower().split())


def _index_names(db_path: Path) -> set:
    """Every filename in the document index, reduced for comparison."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = con.execute("SELECT name FROM files").fetchall()
        con.close()
        return {_comparable(r[0]) for r in rows if r[0]}
    except sqlite3.Error:
        return set()


def main() -> int:
    import config

    summaries_dir = Path(config.PROPERTY_SUMMARIES_DIR)
    if not summaries_dir.is_dir():
        print(f"No property summaries at {summaries_dir}.")
        return 2
    files = [f for f in sorted(summaries_dir.glob("*.md")) if not f.name.startswith("_")]
    if not files:
        print("No property summaries to check.")
        return 2

    index_db = Path(config.BASE_DIR) / "data" / "corpus_index.db"
    names = _index_names(index_db)
    print(f"Checking {len(files)} property summaries against "
          f"{len(names):,} known document names\n")

    texts = {f: f.read_text(encoding="utf-8", errors="replace") for f in files}

    # ---- 1. Every cited document is a real document ------------------------
    print("1. Cited sources are real files")
    if not names:
        skip("every cited document exists in the library",
             "no document index on this machine -- run: python system/main.py index-corpus")
    else:
        missing, total, abbreviated, own = [], 0, 0, 0
        for f, text in texts.items():
            for raw in _CITED_PATH.findall(text):
                # Some citations are deliberately shortened in prose -- a
                # wildcard standing for a family of files, or an ellipsis in a
                # very long name. Those were never meant to resolve, and
                # counting them as fabricated sources would drown out the real
                # ones. Measured on the first run: 35 of 741. Reported, not
                # failed.
                if "*" in raw or "..." in raw or "…" in raw:
                    abbreviated += 1
                    continue
                leaf = _comparable(
                    raw.replace("\\", "/").rstrip("/").split("/")[-1])
                if leaf in _OWN_FILES:
                    own += 1
                    continue
                total += 1
                if leaf not in names:
                    missing.append((f.stem, leaf))
        # Baseline rather than zero, for the same reason the coverage number is
        # a baseline: a suite that is permanently red gets ignored, and this
        # project's checks are trusted precisely because they stay quiet unless
        # something is actually wrong. 25 unresolvable citations existed the day
        # this check was written -- verified as genuine, not an index gap (the
        # index holds paths up to 312 characters, so nothing was truncated, and
        # some cited names have no near match anywhere in the index while others
        # differ from the real filename enough that a reader could not find it).
        # Any NEW one fails immediately, which is the point. Lower this number
        # as they get fixed; never raise it.
        check("no NEW unfindable citations have appeared",
              len(missing) <= _UNRESOLVED_BASELINE,
              f"{total - len(missing)} of {total} exact citations resolve; "
              f"{len(missing)} do not (baseline {_UNRESOLVED_BASELINE})")
        note(f"{abbreviated} citations are deliberately shortened (a wildcard or "
             f"'...'), so they are not expected to resolve")
        note(f"{own} cite a file this system produces itself, not a library "
             f"document, so the library's file list cannot contain them")
        # Group by FILENAME, not by mention. One badly-named file cited on eight
        # bullets used to fill the whole sample eight times over, so a reader saw
        # one property's problem and nothing else -- 26 mentions were only about
        # 20 distinct files, and the other properties were invisible. Naming each
        # file once, with how many bullets lean on it, fits them all.
        by_file = {}
        for stem, leaf in missing:
            by_file.setdefault(leaf, set()).add(stem)
        for leaf in sorted(by_file, key=lambda k: (-len(by_file[k]), k)):
            where = ", ".join(sorted(by_file[leaf]))
            note(f"unresolved: {leaf!r} — cited by {where}")
        note(f"{len(missing)} mentions across {len(by_file)} distinct filenames")

    # ---- 2. Every summary can be currency-checked --------------------------
    # A summary with no source date can never be told apart from a current one,
    # which is the failure mode that produced a confident wrong answer in the
    # first place.
    print("\n2. Every summary can be told whether it is out of date")
    # Two different failures, and they need different fixes, so they are counted
    # separately. Found on the first run: several summaries DO carry a stamp,
    # written as prose ("newest file checked ... is dated 7/16/2025"), which no
    # code can read. Reporting those as "no source date" would have sent
    # someone to add a stamp that is already there.
    no_line, unreadable = [], []
    for f, t in texts.items():
        has_line = re.search(r"Source files as of", t, re.I)
        if not has_line:
            no_line.append(f.stem)
        elif not _STAMP.search(t):
            unreadable.append(f.stem)
    check("every summary says when its sources were read",
          not no_line,
          f"missing the line entirely: {', '.join(no_line[:4])}" if no_line
          else f"all {len(files)}")
    # Baselined for the same reason as the two above: 10 summaries carried a
    # prose stamp the day this was written, and a permanently-red suite stops
    # being read. These are worth fixing by hand -- each is one line, and until
    # then nothing can tell whether those 10 are current. check_system_health
    # reports the same thing to the user for active deals.
    check("no NEW summary has an unreadable source date",
          len(unreadable) <= _UNREADABLE_STAMP_BASELINE,
          f"{len(files) - len(unreadable)} of {len(files)} in YYYY-MM-DD; "
          f"{len(unreadable)} written as prose (baseline {_UNREADABLE_STAMP_BASELINE})"
          + (f" — {', '.join(unreadable[:4])}" if unreadable else ""))

    # ---- 3. Every summary says what it did NOT read ------------------------
    print("\n3. Every summary declares what it did not read")
    no_gaps, empty_gaps = [], []
    for f, text in texts.items():
        # Real headings vary -- "## Gaps", "## Gaps / caveats", "### Gaps (this
        # verification pass)". Demanding an exact match reported two summaries
        # as having no Gaps section when both plainly do: the checker being
        # wrong about the very thing it was checking.
        m = re.search(r"^#{2,4}\s*Gaps\b.*$", text, re.M | re.I)
        if not m:
            no_gaps.append(f.stem)
            continue
        after = text[m.end():]
        body = after.split("\n## ")[0].strip()
        if len(body) < 40:
            empty_gaps.append(f.stem)
    check("every summary has a Gaps section",
          not no_gaps,
          f"missing in: {', '.join(no_gaps[:4])}" if no_gaps else f"all {len(files)}")
    check("  ...and it actually names something",
          not empty_gaps,
          f"empty in: {', '.join(empty_gaps[:4])}" if empty_gaps else "")

    # ---- 4. How much of what is asserted is cited --------------------------
    # Deliberately a measured proportion, not pass/fail. Prose contains
    # connective sentences that correctly carry no citation; a binary rule here
    # would either be noise or be gamed. What matters is the number moving.
    print("\n4. How much of the findings carry a source")
    cited = uncited = 0
    for f, text in texts.items():
        section = re.split(r"^##+\s*Gaps", text, flags=re.M | re.I)[0]
        for line in section.splitlines():
            s = line.strip()
            if not s.startswith(("- ", "* ")) or len(s) < 60:
                continue
            if _CITED_PAGE.search(s) or _CITED_PATH.search(s) or re.search(r"p\.\s*\d", s):
                cited += 1
            else:
                uncited += 1
    total_claims = cited + uncited
    if total_claims:
        pct = cited * 100 // total_claims
        note(f"{cited} of {total_claims} substantive bullets carry a source ({pct}%)")
        check(f"citation coverage has not fallen below the recorded baseline "
              f"({_CITED_BASELINE}%)",
              pct >= _CITED_BASELINE,
              f"now {pct}% — this is a regression detector, not a standard anyone ratified")
    else:
        skip("citation coverage", "no bullets long enough to judge")

    # ---- 5. Build the question set a model-in-the-loop run needs -----------
    # Derived, never stored in the repo: these lines contain real firm facts.
    print("\n5. Question set for the model-in-the-loop run")
    questions = []
    for f, text in texts.items():
        prop = f.stem.replace("-", " ")
        for line in text.splitlines():
            s = line.strip().lstrip("-* ").strip()
            m = _CITED_PAGE.search(s)
            if not m or len(s) < 80 or len(s) > 400:
                continue
            claim = _CITED_PAGE.sub("", s).strip(" .—-")
            questions.append({
                "property": prop,
                "claim": claim,
                "expect_source": m.group(1).strip(),
                "expect_page": m.group(2),
                "kind": "grounded",
            })
    # The other half of the test, and the more important one: does it refuse?
    # Each summary's Gaps section names something genuinely NOT established --
    # asking about those should produce "not established", never an answer.
    for f, text in texts.items():
        # Real headings vary -- "## Gaps", "## Gaps / caveats", "### Gaps (this
        # verification pass)". Demanding an exact match reported two summaries
        # as having no Gaps section when both plainly do: the checker being
        # wrong about the very thing it was checking.
        m = re.search(r"^#{2,4}\s*Gaps\b.*$", text, re.M | re.I)
        if not m:
            continue
        body = text[m.end():].split("\n## ")[0]
        for line in body.splitlines():
            s = line.strip().lstrip("-* ").strip()
            if 60 < len(s) < 300 and "not established" in s.lower():
                questions.append({
                    "property": f.stem.replace("-", " "),
                    "claim": s,
                    "kind": "must_abstain",
                })

    out_dir = Path(config.BASE_DIR) / "data" / "eval"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "question_set.json"
    grounded = sum(1 for q in questions if q["kind"] == "grounded")
    abstain = len(questions) - grounded
    out.write_text(json.dumps(questions, indent=2, ensure_ascii=False), encoding="utf-8")
    check("a question set was built from the summaries themselves",
          grounded > 0 and abstain > 0,
          f"{grounded} answerable, {abstain} that must be refused")
    note(f"written to {out} (gitignored -- it holds real firm facts)")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
