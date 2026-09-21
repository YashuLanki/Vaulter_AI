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

# A citation can fail for two completely different reasons, and until
# 2026-09-21 this check could not tell them apart -- so it reported both as
# "unfindable", which reads as "somebody cited a document that does not exist".
#
# Measured that day: of 12 distinct unresolvable names, EIGHT were documents
# that are still on the drive and had simply been RENAMED -- the firm added a
# "_TS Done" suffix (and sometimes a reviewer marker like "(AHG COMMENTS)") to
# each quarterly report as its review finished. The citation was accurate when
# written. Only four were genuinely absent.
#
# Reporting those eight as fabricated citations is the same failure this whole
# suite exists to catch, pointed at itself: a confident wrong cause. It also
# hides the real four. They are NOT folded into the passing count either -- a
# stale citation is a genuine defect, because a person reading that summary
# cannot find the file under the name given. They get their own category.
#
# The match is deliberately narrow: same extension, the cited name must be a
# PREFIX of the real one, the remainder must begin with a separator, and the
# cited name must be long enough that a prefix match means something. Without
# that last rule a short citation would "recover" to any longer file starting
# the same way, which is how a checker starts inventing reassurance.
_RENAME_MIN_STEM = 12
_RENAME_SEPARATORS = (" ", "(", "_", "-", "[", ".")


def _squash(name: str) -> str:
    """A filename reduced to its letters and digits, so separators stop mattering."""
    return "".join(c for c in name.lower() if c.isalnum())


def _squash_index(names) -> dict:
    """
    Every real filename keyed by its letters and digits alone.

    Values are LISTS, and that is the safety mechanism rather than an
    implementation detail: a key with two or more real files behind it is
    ambiguous, and an ambiguous recovery is refused. This library holds five
    signature pages of one resolution differing only by the signer's surname,
    so a looser match could cheerfully cite the wrong person's signature --
    a confident wrong answer, which is worse than the missing-file report it
    would replace.
    """
    out = {}
    for real in names:
        out.setdefault(_squash(real), []).append(real)
    return out


def _renamed_to(leaf: str, names) -> str:
    """
    The real filename a stale citation almost certainly meant -- the cited name
    plus a suffix -- or "" if there is no such file. Shortest match wins, since
    that is the least-assuming one.
    """
    if "." not in leaf:
        return ""
    stem, _, ext = leaf.rpartition(".")
    if len(stem) < _RENAME_MIN_STEM or not ext:
        return ""
    suffix = "." + ext
    best = ""
    for cand in names:
        if not cand.startswith(stem) or not cand.endswith(suffix):
            continue
        rest = cand[len(stem):-len(suffix)]
        if not rest or not rest.startswith(_RENAME_SEPARATORS):
            continue
        if not best or len(cand) < len(best):
            best = cand
    return best

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

    # SAY HOW OLD THE FILE LIST IS, and prefer the freshest one available.
    #
    # This check asks "does every cited document exist", and the answer is only
    # as good as the list it asks. On 2026-08-24 this machine's development copy
    # had a list 4 days old while a current one sat in the live install, and the
    # check reported citations as unfindable that were sitting right there --
    # the failure went from 7/7 to 6/7 with nothing wrong in the data at all.
    # Two of the five named files were verified present in the current list.
    #
    # That is this project's oldest lesson, in its own house: a freshness claim
    # inherits the freshness of its source. So the age is printed on every run,
    # and where a newer list exists it is used instead of silently trusting the
    # nearer one.
    def _age_days(p):
        if not p.exists():
            return None
        return (_dt.datetime.now()
                - _dt.datetime.fromtimestamp(p.stat().st_mtime)).days

    import datetime as _dt
    alt = Path.home() / "Vaulter AI" / "system" / "data" / "corpus_index.db"
    mine, theirs = _age_days(index_db), _age_days(alt)
    if theirs is not None and (mine is None or theirs < mine):
        here = "absent" if mine is None else f"{mine} day(s)"
        print(f"Using the newer file list found at {alt}")
        print(f"  (that one is {theirs} day(s) old; this folder's is {here} old)")
        index_db = alt
        mine = theirs
    if mine is not None:
        stale = ("  <-- TOO OLD; citation failures below may be its fault, "
                 "not the data's") if mine >= 2 else ""
        print(f"File list is {mine} day(s) old.{stale}")

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

        # Separate "the file is still there under another name" from "the file
        # is not there". Same discipline as _newer_readable_docs' "couldn't
        # check" versus "nothing new": two causes that need two different
        # answers.
        #
        # Two ways a citation goes stale while its document stays put, both
        # measured 2026-09-21:
        #   renamed     -- a suffix was added later ("_TS Done"); 17 mentions.
        #   punctuation -- the citation dropped a separator, so it differs from
        #                  the real name by a hyphen; 1 mention, and it had been
        #                  reported as a fabricated source for weeks.
        squashed = _squash_index(names) if missing else {}
        recoverable, genuinely_missing = [], []
        for who, leaf in missing:
            real = _renamed_to(leaf, names)
            why = "renamed"
            if not real:
                # Exactly one candidate, or none: never guess between two.
                same = squashed.get(_squash(leaf), [])
                if len(same) == 1:
                    real, why = same[0], "punctuation"
            if real:
                recoverable.append((who, leaf, real, why))
            else:
                genuinely_missing.append((who, leaf))
        missing = genuinely_missing
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
        if recoverable:
            distinct = {}
            for who, leaf, real, why in recoverable:
                distinct.setdefault((leaf, real, why), set()).add(who)
            note(f"{len(recoverable)} citation(s) across {len(distinct)} name(s) point "
                 f"at a file that IS still on the drive under a different name -- the "
                 f"document exists, the citation is stale. Not counted as unfindable; "
                 f"fix by correcting the name in the summary, never by deleting the "
                 f"citation")
            for (leaf, real, why), whos in sorted(distinct.items()):
                note(f"  {why}: '{leaf}'")
                note(f"      is really '{real}' (cited by {', '.join(sorted(whos))})")
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

    # ---------------------------------------------------------------- 6 -----
    # The two rules added 2026-09-21, tested on made-up inputs rather than on
    # a real library file. Depending on a particular document would make these
    # checks break the moment somebody renames it -- which is precisely the
    # bug being fixed here, and it would be a poor joke to reintroduce it in
    # the checks for the fix.
    print("\n6. Reading reviewer comments, and recovering a stale citation")

    from corpus.extract import _pdf_date, _NOT_COMMENT_SUBTYPES, _MAX_COMMENTS

    check("a PDF date becomes a plain date",
          _pdf_date(b"D:20260908171335-07'00'") == "2026-09-08",
          _pdf_date(b"D:20260908171335-07'00'"))
    # An unreadable date must come back empty, never as a wrong date -- the
    # same rule the staleness checks follow. A comment's date is the whole
    # basis for judging whether it is news, so a guessed one is worse than none.
    check("...and an unreadable one yields nothing, not a guess",
          all(_pdf_date(x) == "" for x in (None, b"", "rubbish", b"D:xx", 7)))

    # A /Popup is the on-screen bubble belonging to another annotation and a
    # /Link is not a comment at all. One real report carried five sticky notes
    # and five popups; counting the popups would have doubled it.
    check("pop-up bubbles and links are not treated as comments",
          {"popup", "link"} <= _NOT_COMMENT_SUBTYPES)
    check("there is a cap on how many comments one read returns",
          isinstance(_MAX_COMMENTS, int) and 0 < _MAX_COMMENTS <= 200,
          f"{_MAX_COMMENTS} per document")

    # Invented names, deliberately. The rule under test is about SHAPE, and
    # this repo is public -- real filenames are firm detail that does not
    # belong in tracked code.
    real_names = {
        "north field 12 q3 2031 (ok)_ts done.pdf",
        "sample site - utility resolution - alpha sig pg - 2031-01-02.pdf",
        "sample site - utility resolution - bravo sig pg - 2031-01-03.pdf",
        "twin.pdf",
        "twin .pdf",
    }
    check("a citation missing a later suffix is recognised, not called missing",
          _renamed_to("north field 12 q3 2031.pdf", real_names)
          == "north field 12 q3 2031 (ok)_ts done.pdf")
    # Narrow on purpose: a short citation must not "recover" to any longer
    # file that happens to start the same way.
    check("...but a too-short name is never recovered that way",
          _renamed_to("a.pdf", real_names) == "")
    check("...and never across file types",
          _renamed_to("north field 12 q3 2031.docx", real_names) == "")

    idx = _squash_index(real_names)
    check("a citation differing only by punctuation is recognised",
          len(idx.get(_squash(
              "sample site - utility resolution alpha sig pg - 2031-01-02.pdf"), [])) == 1)
    # THE SAFETY MECHANISM. This library holds five signature pages of one
    # resolution differing only by the signer's surname, so a looser match
    # could cite the wrong person's signature -- a confident wrong answer,
    # worse than the missing-file report it replaces. Two candidates means
    # refuse, never pick one.
    check("...but an ambiguous one is refused rather than guessed",
          len(idx.get(_squash("twin.pdf"), [])) == 2)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
