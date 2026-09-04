"""
scripts/check_portfolio_comparison.py
--------------------------------------
Sanity checks for the portfolio comparison matcher (analysis/screening/portfolio_comparison.py).

Run it after ANY change to portfolio_comparison.py or market_eras.py:

    .venv\\Scripts\\python.exe scripts/check_portfolio_comparison.py

There is no pytest in this repo, so this is the safety net for the matcher the
way check_screener.py is the safety net for fit_screen.py. Runs against the
real portfolio comparison index (system/data/portfolio_comparison_index.json)
if it exists; otherwise runs the synthetic-index checks only and says why the
real-data checks were skipped, rather than failing on a fixture that isn't
committed to git (the index contains real firm data on purpose -- see .gitignore).

What these checks protect against, found once already during development:
  * a single soft signal (matching plan_type + a loosely-similar size band)
    scoring high enough to look like a real match -- tested with a
    deliberately unrelated deal (WY agricultural vs. this AZ/CA-heavy
    portfolio) that scored exactly 3 before the threshold was raised to 5;
  * a missing/empty field crashing the scorer instead of just not matching;
  * an empty or missing index crashing instead of returning an honest
    "no comparison data" result.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import analysis.screening.portfolio_comparison as pc  # noqa: E402
from analysis.screening.market_eras import era_for_year, era_note  # noqa: E402

RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, condition, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


# A small synthetic index, independent of the real (gitignored) portfolio
# data, so these specific checks run the same way on every machine including
# a fresh clone with no property summaries seeded yet.
SYNTHETIC_INDEX = [
    {"filename": "a.md", "property_name": "Az Subdivide Co", "state": "AZ", "county": "Pinal",
     "land_type": "residential", "acres": 65, "entry_year": 2013, "plan_type": "subdivide",
     "outcome_status": "still-held", "notes": "test fixture A"},
    {"filename": "b.md", "property_name": "Az Subdivide Near", "state": "AZ", "county": "Pinal",
     "land_type": "residential", "acres": 30, "entry_year": 2014, "plan_type": "subdivide",
     "outcome_status": "sold", "notes": "test fixture B"},
    {"filename": "c.md", "property_name": "Ca Different Everything", "state": "CA", "county": "Kern",
     "land_type": "industrial", "acres": 5000, "entry_year": 2021, "plan_type": "hold-only",
     "outcome_status": "still-held", "notes": "test fixture C, deliberately unrelated"},
    {"filename": "d.md", "property_name": "No Facts At All", "state": "unclear", "county": "unclear",
     "land_type": "unclear", "acres": "unclear", "entry_year": "unclear", "plan_type": "unclear",
     "outcome_status": "unclear", "notes": "test fixture D, everything unclear"},
]


def main() -> int:
    print("1. Synthetic-index checks (run on every machine, no real data needed)")

    r = pc.find_similar_deals(
        {"state": "AZ", "county": "Pinal", "land_type": "residential", "plan_type": "subdivide", "acres": 60},
        index=SYNTHETIC_INDEX)
    check("a strong multi-signal match ranks first",
          bool(r["matches"]) and r["matches"][0]["property_name"] == "Az Subdivide Co",
          f"top match: {r['matches'][0]['property_name'] if r['matches'] else 'NONE'}")
    check("a genuinely unrelated deal does not appear in the results",
          all(m["property_name"] != "Ca Different Everything" for m in r["matches"]))

    # Found 2026-08-06 when this was wired into the real screener: a real
    # CoStar row with no Land Area (AC) value arrives as float("nan"), which
    # passes float() without raising but compares False against every size
    # band including infinity -- crashed the whole screen with StopIteration
    # on the very first deformed export tested, not a rare edge case.
    r_nan_acres = pc.find_similar_deals(
        {"state": "AZ", "land_type": "residential", "acres": float("nan")},
        index=SYNTHETIC_INDEX)
    check("NaN acreage (a real CoStar row with a blank Land Area field) does not crash",
          isinstance(r_nan_acres, dict))

    r_unrelated = pc.find_similar_deals(
        {"state": "WY", "land_type": "agricultural", "plan_type": "hold-only", "acres": 5000},
        index=SYNTHETIC_INDEX)
    check("a deliberately unrelated deal returns no matches, not a forced weak one",
          r_unrelated["matches"] == [],
          f"got {len(r_unrelated['matches'])} matches, coverage note: {r_unrelated['coverage_note'][:60]}...")

    r_empty_facts = pc.find_similar_deals({}, index=SYNTHETIC_INDEX)
    check("empty facts dict does not crash, returns no forced matches",
          r_empty_facts["matches"] == [])

    r_all_unclear_row = pc.find_similar_deals(
        {"state": "AZ", "county": "Pinal", "land_type": "residential", "plan_type": "subdivide", "acres": 60},
        index=SYNTHETIC_INDEX)
    check("a record with every field 'unclear' never appears as a match",
          all(m["property_name"] != "No Facts At All" for m in r_all_unclear_row["matches"]))

    r_no_index = pc.find_similar_deals({"state": "AZ"}, index=[])
    check("an empty index returns an honest message, not a crash",
          r_no_index["matches"] == [] and "No portfolio comparison data" in r_no_index["coverage_note"])

    print("\n2. Market-era timeline checks")
    check("a known year (2011) resolves to the post-crash-bottom era",
          era_for_year(2011) is not None and "2010" in era_for_year(2011)["label"] + str(era_for_year(2011)),
          str(era_for_year(2011)["label"] if era_for_year(2011) else None))
    check("'unclear' as a year does not crash, returns no note",
          era_note("unclear") == "")
    check("None as a year does not crash, returns no note",
          era_note(None) == "")
    check("a year far outside the timeline (1850) returns None, not a crash",
          era_for_year(1850) is None)
    # Every year the portfolio could plausibly have a deal in (1999-2026) must
    # resolve to exactly one era -- a gap in the table would silently drop the
    # era_note for any real deal from that year.
    gaps = [y for y in range(1999, 2027) if era_for_year(y) is None]
    check("no year-gaps in the timeline across the portfolio's actual date range (1999-2026)",
          gaps == [], f"gaps at: {gaps}")

    print("\n3. Real portfolio index (if built)")
    real_index = pc.load_index()
    if not real_index:
        print("  SKIP  real-index checks — system/data/portfolio_comparison_index.json not found "
              "(gitignored; run the extraction pass to build it on this machine)")
    else:
        check(f"real index loads and has a plausible number of properties ({len(real_index)})",
              10 <= len(real_index) <= 500)
        bad_land = [r["filename"] for r in real_index if r.get("land_type") not in pc.LAND_TYPES]
        check("every real record has a recognized land_type",
              bad_land == [], f"bad rows: {bad_land}")
        bad_plan = [r["filename"] for r in real_index if r.get("plan_type") not in pc.PLAN_TYPES]
        check("every real record has a recognized plan_type",
              bad_plan == [], f"bad rows: {bad_plan}")
        bad_outcome = [r["filename"] for r in real_index if r.get("outcome_status") not in pc.OUTCOME_STATUSES]
        check("every real record has a recognized outcome_status",
              bad_outcome == [], f"bad rows: {bad_outcome}")

        # Provenance, added 2026-08-11. A classification with no recorded
        # source was measured wrong 2 times in 3; the point of the field is
        # that a reader is TOLD that, so a record silently losing it is a
        # regression worth failing on.
        SOURCES = {"documents", "summary", "unrecorded"}
        bad_src = [r["filename"] for r in real_index
                   if r.get("plan_type_source") not in SOURCES]
        check("every real record records how its plan_type was derived",
              bad_src == [], f"bad rows: {bad_src}")
        check("an unrecorded-provenance match is flagged as unconfirmed",
              "[unconfirmed]" in pc.summarize_match(
                  {"property_name": "X", "plan_type": "subdivide",
                   "outcome_status": "still-held", "notes": "n",
                   "plan_type_source": "unrecorded"}))
        # "[verified]" was removed 2026-08-12: after the provenance re-read it
        # appeared on 216 of 216 rows of a real export, so it distinguished
        # nothing and its explanatory note in the report read as confusing.
        # Only the weak end is marked now.
        _doc_line = pc.summarize_match(
            {"property_name": "X", "plan_type": "subdivide",
             "outcome_status": "still-held", "notes": "n",
             "plan_type_source": "documents"})
        check("a document-verified match carries NO tag at all",
              "[verified]" not in _doc_line and "[unconfirmed]" not in _doc_line,
              f"got: {_doc_line}")
        check("no match of any provenance re-introduces a [verified] tag",
              not any("[verified]" in pc.summarize_match(
                  {"property_name": "X", "plan_type": "subdivide",
                   "outcome_status": "still-held", "notes": "n",
                   "plan_type_source": s}) for s in ("documents", "summary", "unrecorded", "")))
        check("a summary-derived match carries neither tag",
              not any(t in pc.summarize_match(
                  {"property_name": "X", "plan_type": "subdivide",
                   "outcome_status": "still-held", "notes": "n",
                   "plan_type_source": "summary"})
                  for t in ("[verified]", "[unconfirmed]")))

        # ── Property ID registry ────────────────────────────────────────
        # Added 2026-08-11. Its only job is that every name in every data
        # source resolves to exactly one property, so the checks are: does it
        # cover the whole portfolio, and does every source name resolve. The
        # build caught a real merge on first run (a project and its later
        # phase share a name stem), so the distinctness case is asserted
        # explicitly rather than assumed.
        try:
            import config as _cfg
            import portfolio as _pf
            from pipeline import property_registry as _reg

            registry = _reg.load_registry(_cfg.DATA_DIR)
            if not registry:
                print("  SKIP  property-ID checks — no registry built on this machine")
            else:
                active, _sold = _pf.load_properties()
                canon = {r["canonical_name"] for r in registry.values()}
                missing = [p["name"] for p in active if p["name"] not in canon]
                check("every active property has its own ID",
                      missing == [], f"missing: {missing}")
                check("no two properties share an ID",
                      len(canon) == len(registry))
                unresolved = [r["property_name"] for r in real_index
                              if _reg.resolve(_cfg.DATA_DIR, r["property_name"],
                                              registry=registry) is None]
                check("every comparison-index name resolves to a property",
                      unresolved == [], f"unresolved: {unresolved}")
                # Finds its own test case from the registry rather than naming a
                # real property here -- this file is public. The bug this guards
                # against (a project and its later-phase sibling sharing a name
                # stem being merged into one ID) is structural, not tied to any
                # one property, so any real pair with that shape proves the point.
                names = [rec["canonical_name"] for rec in registry.values()]
                stem_pair = None
                for n in names:
                    longer = [m for m in names if m != n and m.startswith(n)]
                    if longer:
                        stem_pair = (n, longer[0])
                        break
                if stem_pair:
                    a = _reg.resolve(_cfg.DATA_DIR, stem_pair[0], registry=registry)
                    b = _reg.resolve(_cfg.DATA_DIR, stem_pair[1], registry=registry)
                    check("a project and its later phase get distinct IDs",
                          a is not None and b is not None and a != b,
                          f"ids: {a} vs {b}")
                else:
                    print("  SKIP  no name-stem pair found in the current registry to test against")
        except Exception as e:
            check("property-ID registry checks ran", False, f"{type(e).__name__}: {e}")
        r_real = pc.find_similar_deals(
            {"state": "AZ", "county": "Pinal", "land_type": "residential", "plan_type": "subdivide", "acres": 50},
            index=real_index)
        check("a realistic AZ/Pinal query against the real index returns at least one match",
              len(r_real["matches"]) > 0, f"{len(r_real['matches'])} matches")

    # ── 4. Summary currency checks ────────────────────────────────
    # These guard the start-of-conversation warning that a property summary
    # has fallen behind its documents. The failure that matters here is not a
    # crash -- it is silence: a summary that IS behind reported as fine, or a
    # check that could not run reported as "nothing new". Both produce a
    # confident, well-cited, months-out-of-date answer, which is the worst
    # shape a wrong answer can take. Added 2026-08-11 after exactly that
    # happened on a live deal.
    print("\n4. Summary currency checks")
    try:
        import datetime as _dt
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import mcp_server as _m
        import config as _c

        check("a normal 'Source files as of' stamp parses",
              _m._summary_stamp("**Source files as of:** 2026-08-03 (mtime of...)") is not None)
        check("a summary with no stamp yields None, not a guessed date",
              _m._summary_stamp("no stamp anywhere in this text") is None)
        check("an impossible date (month 13) yields None, not a crash",
              _m._summary_stamp("Source files as of: 2026-13-45") is None)

        _st = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

        # A NAME THAT MATCHES NOTHING IS "CANNOT TELL", NOT "NOTHING NEWER".
        # This check used to assert the opposite -- it passed a made-up property
        # name and demanded 0 -- which encoded the bug rather than catching it.
        # A property that does not exist on the drive does not have "genuinely no
        # newer documents"; nothing was looked at. Measured 2026-09-01: 19 of 49
        # real Project Master names match no folder, and every one of them was
        # being reported as current.
        _absent = _m._newer_readable_docs("Zzz No Such Property Zzz", _st)
        check("a name matching NOTHING on the drive reports 'cannot tell', not 0",
              _absent is None, f"got {_absent!r}")

        # And the distinction the old check meant to make, tested properly: a
        # property that really is on the drive, with a stamp far in the future so
        # nothing can be newer, must report 0 -- checked, genuinely nothing.
        _real_name = None
        try:
            _act, _ = __import__("portfolio").load_properties()
            for _p in _act:
                if _m._newer_readable_docs(_p["name"],
                                           _dt.datetime(2099, 1, 1)) is not None:
                    _real_name = _p["name"]
                    break
        except Exception:
            pass
        if _real_name:
            _clean = _m._newer_readable_docs(_real_name, _dt.datetime(2099, 1, 1))
            check("a REAL property with nothing newer reports 0, not None",
                  _clean is not None and _clean[0] == 0,
                  f"{_real_name}: got {_clean!r}")
        else:
            check("a REAL property with nothing newer reports 0, not None",
                  False, "could not find a resolvable property to test with")

        # The new helper answers only "was anything looked at".
        check("the no-files helper spots a name that matches nothing",
              "Zzz No Such Property Zzz" in
              _m._properties_with_no_files(["Zzz No Such Property Zzz"]))

        # THE one that matters most: with no index to read, the answer is
        # "cannot tell" (None) and must never collapse into "nothing new" (0).
        _real_idx = _c.CORPUS_INDEX_FILE
        try:
            _c.CORPUS_INDEX_FILE = Path(r"C:\nope\definitely_missing_index.db")
            _cannot = _m._newer_readable_docs("Anything At All", _st)
        finally:
            _c.CORPUS_INDEX_FILE = _real_idx
        check("with no document list, 'cannot check' stays None and never becomes 0",
              _cannot is None, f"got {_cannot!r}")

        check("the active-deal stage list is non-empty and holds real stage names",
              bool(_m.ACTIVE_DEAL_STAGES) and "Acquisition" in _m.ACTIVE_DEAL_STAGES,
              f"{_m.ACTIVE_DEAL_STAGES}")

        check("batching with nothing to look up returns empty, not None",
              _m._newest_docs_for_many({}) == {})

        _real_idx = _c.CORPUS_INDEX_FILE
        try:
            _c.CORPUS_INDEX_FILE = Path(r"C:\nope\definitely_missing_index.db")
            _batch_blind = _m._newest_docs_for_many({"Anything": _st})
        finally:
            _c.CORPUS_INDEX_FILE = _real_idx
        check("batching with no document list returns None, never an empty result",
              _batch_blind is None, f"got {_batch_blind!r}")

        # The fast batched path replaced a slow per-property one (4.9s -> 0.3s,
        # which mattered: it had pushed check_system_health past its 15s bar).
        # Speed is only worth having if the answer is identical, so prove it
        # against the real index rather than assuming.
        if Path(_c.CORPUS_INDEX_FILE).exists():
            _recs = real_index if isinstance(real_index, list) else list(real_index.values())
            # An old date so real properties genuinely have newer documents --
            # otherwise both sides return nothing and the comparison passes
            # while proving nothing, which is the "fast empty answer" failure
            # this project has been bitten by three times.
            _probe = {r["property_name"]: _dt.datetime(2015, 1, 1, tzinfo=_dt.timezone.utc)
                      for r in _recs[:8] if r.get("property_name")}
            _batched = _m._newest_docs_for_many(_probe) or {}
            _one_by_one = {}
            for _nm, _when in _probe.items():
                _c2 = _m._newer_readable_docs(_nm, _when)
                if _c2 and _c2[0] and _c2[1]:
                    _one_by_one[_nm] = _c2[1][0][0]
            check("the equivalence probe actually found documents to compare",
                  len(_batched) > 0,
                  f"{len(_batched)} of {len(_probe)} probed properties had newer docs")
            check("batched lookup agrees exactly with the per-property lookup",
                  _batched == _one_by_one,
                  f"batched={len(_batched)} single={len(_one_by_one)}"
                  + ("" if _batched == _one_by_one
                     else f" DIFF={set(_batched.items()) ^ set(_one_by_one.items())}"))
    except Exception as e:
        check("summary currency checks ran", False, f"{type(e).__name__}: {e}")

    # ── 4a. Exit figures are recorded honestly ────────────────────
    # Added 2026-08-12. These make outcome QUALITY visible for the first time,
    # and the danger is presenting a gross price comparison as though it were a
    # return: Banning sold at 1.76x over ten years, which before entitlement
    # spend, carry and taxes may well not be a profit at all. If these are ever
    # to inform ranking, that distinction must survive.
    print("\n4a. Exit figures are recorded honestly")
    try:
        _sold = [r for r in real_index if r.get("outcome_status") == "sold"]
        check("every sold deal now carries an exit year and hold",
              all(r.get("exit_year") and r.get("hold_years") for r in _sold),
              f"{len(_sold)} sold records")
        check("the multiple is named as a GROSS PRICE multiple, never a return",
              all("gross_price_multiple" in r for r in _sold)
              and not any("return" in k or "moic" in k.lower() for r in _sold for k in r),
              "a gross price comparison is not a realised return")
        # A deal whose entry basis is unknown must not get a fabricated multiple.
        _no_entry = [r for r in _sold
                     if not isinstance(r.get("entry_price_usd"), (int, float))]
        check("a deal with no known entry basis gets NO multiple",
              all(r.get("gross_price_multiple") is None for r in _no_entry),
              f"{len(_no_entry)} record(s) with an unclear entry price")
        # The finding that matters: selling is not the same as succeeding.
        _mult = {r["property_name"]: r.get("gross_price_multiple") for r in _sold
                 if r.get("gross_price_multiple")}
        check("the record still shows a sold deal can be a weak outcome",
              any(m < 2.0 for m in _mult.values()),
              "at least one exit is under 2x gross -- 'sold' must never be read as 'good'")
    except Exception as e:
        check("exit-figure checks ran", False, f"{type(e).__name__}: {e}")

    # ── 4b. Updates only ever go forwards ─────────────────────────
    # Git short hashes carry no order, so the update check could only ask "is
    # this different?" and would happily offer an OLDER release -- measured
    # 2026-08-12 when a real fresh install came up newer than the channel and
    # was offered a downgrade, which would have silently removed the fixes it
    # had just been sent to deliver. Every branch is asserted here because the
    # blast radius is every teammate's machine at once.
    print("\n4b. Updates only ever go forwards")
    try:
        import datetime as _d2
        _VF = Path(__file__).resolve().parent.parent / "VERSION"
        _had = _VF.exists()
        _backup = _VF.read_text(encoding="utf-8") if _had else None
        NEW = "2026-08-12T10:00:00-07:00"
        OLD = "2026-08-01T10:00:00-07:00"

        def _with_version(text):
            if text is None:
                _VF.unlink(missing_ok=True)
            else:
                _VF.write_text(text, encoding="utf-8")

        def _offer(local, marker):
            _with_version(local)
            return _m._published_is_newer(marker)

        try:
            check("an OLDER published release is never offered",
                  _offer(f"aaa\n{NEW}\n", {"version": "old", "commit_time": OLD}) is False)
            check("a NEWER published release is offered",
                  _offer(f"aaa\n{OLD}\n", {"version": "new", "commit_time": NEW}) is True)
            check("the same date is not treated as newer",
                  _offer(f"aaa\n{NEW}\n", {"version": "other", "commit_time": NEW}) is False)
            check("an install predating dated VERSION still gets updates",
                  _offer("aaa", {"version": "new", "commit_time": NEW}) is True)
            check("a marker predating dated VERSION is treated as older",
                  _offer(f"aaa\n{NEW}\n", {"version": "old"}) is False)
            check("--force still allows a deliberate rollback",
                  _offer(f"aaa\n{NEW}\n",
                         {"version": "old", "commit_time": OLD, "force": True}) is True)
            check("an unreadable date refuses rather than crashing",
                  _offer(f"aaa\n{NEW}\n",
                         {"version": "x", "commit_time": "not-a-date"}) is False)
            check("no VERSION file at all still gets updates",
                  _offer(None, {"version": "x", "commit_time": NEW}) is True)
            check("the version string itself ignores the date line",
                  _offer(f"abc1234\n{NEW}\n", {"version": "x", "commit_time": NEW}) is not None
                  and _m._get_code_version() == "abc1234",
                  f"read {_m._get_code_version()!r}")
        finally:
            _with_version(_backup if _had else None)
    except Exception as e:
        check("update-direction checks ran", False, f"{type(e).__name__}: {e}")

    # ── 5. Document-library detection ─────────────────────────────
    # The library's folder name is deliberately NOT in the code (this repo is
    # public and the name identifies the firm's SharePoint site), so it is
    # found by shape instead: OneDrive names a synced library "<Org> - <Site>"
    # while personal folders are plain single names. That makes detection
    # load-bearing -- get it wrong and the system either finds nothing or,
    # far worse, points at the individual's own Desktop/Documents. These run
    # against throwaway folders, never the real OneDrive.
    print("\n5. Document-library detection")
    try:
        import shutil as _shutil
        import tempfile as _tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import config as _cf

        def _fake_root(*names):
            d = Path(_tempfile.mkdtemp(prefix="vlt_cfg_"))
            for n in names:
                (d / n).mkdir()
            return d

        personal = ("Desktop", "Documents", "Pictures",
                    "Microsoft Teams Chat Files", "Attachments")

        # These checks are about the rules for reading folders off disk, so
        # OneDrive's own records are switched off for them -- otherwise they all
        # return THIS machine's real library and pass or fail for the wrong
        # reason (which is exactly what happened when the records lookup started
        # working without a configured address, 2026-08-19). The records route
        # gets its own check immediately below, rather than going uncovered.
        _real_records = _cf._library_from_onedrive_records

        r = _fake_root("Desktop", "Acme Co - alpha")
        (Path(r) / "Acme Co - alpha" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        elsewhere = Path(_tempfile.mkdtemp(prefix="vlt_elsewhere_"))
        (elsewhere / "zeta" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            _cf._library_from_onedrive_records = lambda: elsewhere / "zeta"
            got = _cf._find_corpus_subfolder(r)
            check("what OneDrive itself reports wins over anything found on disk",
                  got is not None and got.name == "zeta",
                  f"got {got.name if got else None}")
            check("  ...which is how a library on another drive is found at all",
                  got is not None and not str(got).startswith(str(r)))
        finally:
            _cf._library_from_onedrive_records = _real_records
            _shutil.rmtree(r, ignore_errors=True)
            _shutil.rmtree(elsewhere, ignore_errors=True)

        # The marker is a strong signal but must never override the privacy
        # boundary. A real teammate had an EMPTY marker folder at her OneDrive
        # ROOT, left by her own install, and "the folder containing the marker"
        # therefore described her whole account root -- Desktop, Documents,
        # Pictures and all. Caught 2026-08-19 before it reached her.
        r = _fake_root("Desktop", "Documents", "Pictures", _cf.SHARED_SUBFOLDER)
        (Path(r) / "Documents" / "zeta" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("an empty marker at the account root never makes the ROOT the library",
                  got is None or got.resolve() != Path(r).resolve(),
                  f"got {got}")
            check("  ...and a personal folder is never returned as the library",
                  got is None or not (got / "Desktop").is_dir())
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # A matching library ADDRESS says which library, never which folder
        # level this machine mounted. OneDrive lets you sync a whole library or
        # one folder inside it, and both record the same address -- so on a real
        # teammate's machine the address matched a mount one level ABOVE the
        # firm's folder, and everything she needed looked missing while sitting
        # right there. _narrow_to_library is what stops that.
        r = _fake_root("Desktop", "Documents", "Acme Co - Documents")
        (Path(r) / "Acme Co - Documents" / "riverbend" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._narrow_to_library(Path(r) / "Acme Co - Documents")
            check("a mount one level above the library narrows down to it",
                  got is not None and got.name == "riverbend",
                  f"got {got.name if got else None}")
            inner = Path(r) / "Acme Co - Documents" / "riverbend"
            check("  ...and a mount that already IS the library is left alone",
                  _cf._narrow_to_library(inner) == inner)
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # Finding the folder must never leave a complaint behind. Detection notes
        # WHY it could not identify the library and the wizard reads that note --
        # so a stale note after a SUCCESSFUL find made setup tell a real teammate
        # her library was not on her computer while having just found it, and sent
        # her off to re-sync OneDrive for no reason. Caught 2026-08-20.
        r = _fake_root("Desktop", "Documents", "Acme Co - Documents")
        (Path(r) / "Acme Co - Documents" / "riverbend" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        _saved_sub = _cf.CORPUS_SUBFOLDER
        _cf.CORPUS_SUBFOLDER = "Acme Co - riverbend"      # a name that is NOT there
        try:
            got = _cf._find_corpus_subfolder(r)
            check("finding the folder clears any earlier complaint",
                  got is not None and not _cf.CORPUS_UNRESOLVED_REASON,
                  f"found {got.name if got else None}, "
                  f"complaint {_cf.CORPUS_UNRESOLVED_REASON!r}")
        finally:
            _cf.CORPUS_SUBFOLDER = _saved_sub
            _shutil.rmtree(r, ignore_errors=True)

        _cf._library_from_onedrive_records = lambda: None

        # Found by its own distinctive word, for when our marker folder has not
        # synced into the library yet. Second to the marker, never ahead of it.
        _saved_hint = os.environ.get("VAULTER_CORPUS_HINT")
        os.environ["VAULTER_CORPUS_HINT"] = "riverbend"
        try:
            r = _fake_root("Desktop", "Acme Co - Documents")
            (Path(r) / "Acme Co - Documents" / "riverbend" / "!PROPERTIES").mkdir(parents=True)
            try:
                got = _cf._find_corpus_subfolder(r)
                check("the library is found by its distinctive word, marker or not",
                      got is not None and got.name == "riverbend",
                      f"got {got.name if got else None}")
            finally:
                _shutil.rmtree(r, ignore_errors=True)

            # Two folders carrying the word is ambiguous, and ambiguous means stop.
            r = _fake_root("Desktop", "Acme Co - Documents", "Other Org - Docs")
            (Path(r) / "Acme Co - Documents" / "riverbend" / "!P").mkdir(parents=True)
            (Path(r) / "Other Org - Docs" / "riverbend archive" / "!P").mkdir(parents=True)
            try:
                check("  ...but two folders with that word refuses rather than guessing",
                      _cf._find_corpus_subfolder(r) is None)
            finally:
                _shutil.rmtree(r, ignore_errors=True)
        finally:
            if _saved_hint is None:
                os.environ.pop("VAULTER_CORPUS_HINT", None)
            else:
                os.environ["VAULTER_CORPUS_HINT"] = _saved_hint


        r = _fake_root(*personal, "Acme Co - somelibrary")
        try:
            got = _cf._find_corpus_subfolder(r)
            check("one library among personal folders is found",
                  got is not None and got.name == "Acme Co - somelibrary",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        r = _fake_root(*personal)
        try:
            check("no library synced returns None, never a personal folder",
                  _cf._find_corpus_subfolder(r) is None)
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # Refusing to guess matters more than picking: the wrong library
        # would silently index someone else's site.
        r = _fake_root("Desktop", "Acme Co - alpha", "Acme Co - beta")
        try:
            check("two libraries: refuses to guess between them",
                  _cf._find_corpus_subfolder(r) is None)
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # OneDrive only names a library "<Org> - <Site>" when it is added with
        # "Sync". "Add shortcut to My files" names the folder after the library
        # itself -- no " - " anywhere. Until 2026-08-13 the content check ran
        # only against names already matching the " - " shape, so a library
        # added that way was invisible and the whole thing refused. Measured on
        # a real teammate's machine. Content must beat name, and be tried first.
        r = _fake_root("Desktop", "Documents", "projectfiles")
        (Path(r) / "projectfiles" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("a library whose name has no ' - ' is still found, by content",
                  got is not None and got.name == "projectfiles",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # The library is not always at the account root. Sync the parent site's
        # default "Documents" library instead of the firm's own, and the firm's
        # library arrives as a folder INSIDE it -- same documents, one level
        # down. Measured on a real teammate's machine 2026-08-18; detection
        # only ever looked at the top level, so it matched the parent by name
        # shape and indexed that, finding her property documents but never the
        # team folder below them.
        r = _fake_root("Desktop", "Documents", "Acme Co - Documents")
        (Path(r) / "Acme Co - Documents" / "alpha" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("a library nested one level down is still found",
                  got is not None and got.name == "alpha",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # ...and inside a folder OneDrive made for the individual, which is a
        # DIFFERENT case from the one above and was still broken after it.
        # The nested search walked only the folders eligible to BE a library,
        # and that list excludes Desktop/Documents/Pictures/... -- so a library
        # sitting inside a folder literally named `Documents` was skipped
        # before the search started. Measured on a second teammate's machine
        # 2026-08-19: her layout was not found, while both layouts above were.
        #
        # Descending into a personal folder is not indexing one. Only a CHILD
        # holding the team's shared folder is ever accepted, which is a signal
        # this system put there rather than a guess about what a folder holds.
        r = _fake_root("Desktop", "Pictures", "Documents")
        (Path(r) / "Documents" / "gamma" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("a library nested inside a personal folder is still found",
                  got is not None and got.name == "gamma",
                  f"got {got.name if got else None}")
            check("  ...and the personal folder itself is never returned",
                  got is not None and got.name.lower() != "documents",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # A personal folder with no library inside it must still be ignored --
        # the fix above must not turn "descend into it" into "treat it as one".
        r = _fake_root("Desktop", "Documents")
        (Path(r) / "Documents" / "Some Personal Project").mkdir(parents=True)
        try:
            check("a personal folder with no library in it is still not the library",
                  _cf._find_corpus_subfolder(r) is None)
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        # How deep the search reaches, and that it STOPS. Both matter: a library
        # two or three folders down is a real layout, and an unbounded walk would
        # list the library's own hundreds of thousands of placeholder files at
        # the start of every conversation. The measured reach is four levels
        # below the OneDrive root; five is refused rather than chased.
        r = _fake_root("Desktop", "Documents")
        (Path(r) / "Documents" / "Work" / "Clients" / "delta"
         / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("a library four levels down is still found",
                  got is not None and got.name == "delta",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        r = _fake_root("Desktop", "a")
        (Path(r) / "a" / "b" / "c" / "d" / "epsilon"
         / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            check("  ...but the search gives up rather than walking forever",
                  _cf._find_corpus_subfolder(r) is None)
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        r = _fake_root("Desktop", "Other Org - Team", "Acme Co - beta", "sharepointdocs")
        (Path(r) / "sharepointdocs" / _cf.SHARED_SUBFOLDER).mkdir(parents=True)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("  ...even alongside two normally-named libraries that are not ours",
                  got is not None and got.name == "sharepointdocs",
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        r = _fake_root("Desktop", "Acme Co - alpha", _cf.SHARED_SUBFOLDER)
        try:
            got = _cf._find_corpus_subfolder(r)
            check("this system's own shared folder is never mistaken for the library",
                  got is not None and got.name != _cf.SHARED_SUBFOLDER,
                  f"got {got.name if got else None}")
        finally:
            _shutil.rmtree(r, ignore_errors=True)

        check("the library folder name is not hardcoded in config.py",
              _cf.CORPUS_SUBFOLDER == "" or bool(os.environ.get("VAULTER_CORPUS_SUBFOLDER")),
              "it must come from confidentials/.env, never the tracked source")

        # The org's own name is not in the code either, so the account-root
        # fallback must work for ANY organisation. Env vars are stripped here
        # so only the glob path can answer -- otherwise this machine's real
        # OneDrive would satisfy the check without exercising the fallback.
        _saved_env = {v: os.environ.pop(v, None)
                      for v in ("OneDriveCommercial", "OneDrive")}
        _saved_profile = os.environ.get("USERPROFILE")
        try:
            r = _fake_root("Documents", "OneDrive - Some Other Org")
            os.environ["USERPROFILE"] = str(r)
            got = _cf._detect_onedrive_root()
            check("the OneDrive account root is found for any organisation, not just ours",
                  got is not None and got.name == "OneDrive - Some Other Org",
                  f"got {got.name if got else None}")
            _shutil.rmtree(r, ignore_errors=True)

            r = _fake_root("Documents")
            os.environ["USERPROFILE"] = str(r)
            check("a profile with no OneDrive returns None, not a wrong folder",
                  _cf._detect_onedrive_root() is None)
            _shutil.rmtree(r, ignore_errors=True)
        finally:
            for v, was in _saved_env.items():
                if was is not None:
                    os.environ[v] = was
            if _saved_profile is not None:
                os.environ["USERPROFILE"] = _saved_profile
            _cf._library_from_onedrive_records = _real_records
    except Exception as e:
        check("document-library detection checks ran", False, f"{type(e).__name__}: {e}")

    # -- 6. Being TOLD an update is waiting ------------------------
    # Announcing a waiting update used to live only in check_system_health,
    # whose automatic call is a REQUEST to Claude, not a guarantee. Measured
    # 2026-09-02 on this machine's own log: 82 server sessions, 12 called any
    # tool at all, and 2 of those began with the health check. So the notice
    # now rides along on ordinary tool answers, and these checks are what stop
    # it becoming either silent or a nag.
    print()
    print("6. Being told an update is waiting")
    try:
        import json as _j
        import time as _tm
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import mcp_server as _ms
        import config as _cfg

        _ready = Path(_cfg.PENDING_UPDATE_DIR) / "ready.json"
        _stamp = Path(_cfg.UPDATE_CHECK_STAMP_FILE)
        _was_ready = _ready.read_bytes() if _ready.exists() else None
        _was_stamp = _stamp.read_bytes() if _stamp.exists() else None
        _body = "ANSWER" * 40

        def _plant(v):
            _ready.parent.mkdir(parents=True, exist_ok=True)
            _ready.write_text(_j.dumps({"version": v, "zip_filename": "f.zip",
                                        "signature": "x"}))

        def _freeze():
            # Stop the channel from being asked, so these test the NOTICE
            # itself and never depend on what happens to be published today.
            _stamp.parent.mkdir(parents=True, exist_ok=True)
            _stamp.write_text("frozen")
            os.utime(_stamp, (_tm.time(), _tm.time()))

        try:
            _freeze()
            _plant("zzzzzz9")
            out = _ms._with_pending_notice("get_screening_rules", _body)
            check("an ordinary tool answer says an update is waiting",
                  "ready to install" in out and "zzzzzz9" in out)
            check("...and it tells Claude to ask the user before applying",
                  "apply_pending_update" in out and "without asking" in out)
            check("...and the tool's own answer survives intact",
                  out.startswith(_body))

            check("check_system_health is never double-noticed",
                  _ms._with_pending_notice("check_system_health", _body) == _body)
            check("a structured (non-text) answer is never touched",
                  _ms._with_pending_notice("x", {"a": 1}) == {"a": 1})

            # The phantom-update false alarm this project has already paid for
            # once: a machine that is fully current must not be told to update.
            _freeze()
            _plant(_ms._get_code_version())
            check("a machine already on the staged version is not nagged",
                  "ready to install" not in _ms._with_pending_notice("x", _body))

            _freeze()
            _ready.unlink()
            check("nothing staged means nothing said",
                  "ready to install" not in _ms._with_pending_notice("x", _body))

            # A wrong SHAPE is a third failure mode next to missing and
            # unparseable, and it must never cost the answer.
            _bad_ok = True
            for _bad in ("[1,2,3]", "null", chr(34) + "text" + chr(34), "not json {"):
                _freeze()
                _ready.write_text(_bad)
                if _ms._with_pending_notice("x", _body) != _body:
                    _bad_ok = False
                    break
            check("a wrong-shaped or unparseable marker cannot alter the answer",
                  _bad_ok, "tried a list, null, a string and broken JSON")

            # "Cannot tell" here must mean ASK, which is the OPPOSITE direction
            # from _newer_readable_docs, and deliberately so: asking again costs
            # one folder read, while not asking is the silence this exists to
            # end.
            if _ready.exists():
                _ready.unlink()
            if _stamp.exists():
                _stamp.unlink()
            check("with no stamp at all, the channel is asked rather than skipped",
                  _ms._update_check_due() is True)
            _freeze()
            check("a stamp written just now stops it asking again",
                  _ms._update_check_due() is False)
            _old = _tm.time() - 7 * 3600
            os.utime(_stamp, (_old, _old))
            check("a stamp older than the gate makes it ask again",
                  _ms._update_check_due() is True, "7 hours old")
        finally:
            if _was_ready is None:
                if _ready.exists():
                    _ready.unlink()
            else:
                _ready.write_bytes(_was_ready)
            if _was_stamp is None:
                if _stamp.exists():
                    _stamp.unlink()
            else:
                _stamp.write_bytes(_was_stamp)
    except Exception as e:
        check("update-notice checks ran", False, f"{type(e).__name__}: {e}")

    # -- 7. Setup puts the person on the team's install list ---------
    # The list used to be written from ONE place, check_system_health, inside a
    # conversation. So somebody appeared on it when they first USED the system,
    # never when they installed it. Measured: a teammate who installed on
    # 2026-08-20 and never opened a conversation was still absent on
    # 2026-09-02, indistinguishable from someone who never installed -- while
    # his setup log sat in the team folder the whole time.
    print()
    print("7. Setup puts the person on the team's install list")
    try:
        import inspect as _insp
        import io as _io
        import json as _js
        import shutil as _sh
        import tempfile as _tf
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import config as _cfg
        import mcp_server as _ms
        import setup_wizard as _sww

        # The wiring itself. A step that exists but is never run is the easy
        # way for this to quietly stop happening again.
        _src = _insp.getsource(_sww._run_setup)
        check("setup actually RUNS the registration step",
              "register_install()" in _src)
        check("...and reports it in the summary like every other step",
              "results[" in _src and "register_install()" in _src)

        # It must not assemble a second copy of the record. Two functions
        # answering the same question is how a rule added to one becomes a bug
        # in the other -- already paid for once here, in the two staleness
        # checks.
        _reg = _insp.getsource(_sww.register_install)
        check("it calls the server's own writer rather than duplicating it",
              "_write_install_checkin" in _reg)
        check("...and never builds its own record shape",
              "format_version" not in _reg and "last_seen" not in _reg)

        def _run(where, fallback):
            _saved = (_cfg.INSTALLS_DIR, _cfg.SHARED_DIR_IS_FALLBACK,
                      sys.stdout, sys.stderr)
            _st = Path(_cfg.CHECKIN_STAMP_FILE)
            _wasst = _st.read_bytes() if _st.exists() else None
            if _st.exists():
                _st.unlink()          # so the once-a-day gate does not skip it
            _cfg.INSTALLS_DIR = where
            _cfg.SHARED_DIR_IS_FALLBACK = fallback
            _con = _io.StringIO()
            sys.stdout = _con
            sys.stderr = _con
            try:
                return _sww.register_install(), _con.getvalue()
            finally:
                (_cfg.INSTALLS_DIR, _cfg.SHARED_DIR_IS_FALLBACK,
                 sys.stdout, sys.stderr) = _saved
                if _wasst is None:
                    if _st.exists():
                        _st.unlink()
                else:
                    _st.write_bytes(_wasst)

        # A throwaway folder, never the team's own -- these checks must not
        # write into a folder the whole team reads.
        _d = Path(_tf.mkdtemp())
        try:
            _ok, _out = _run(_d, False)
            _files = list(_d.glob("*.json"))
            check("registering writes a record when the team folder is reachable",
                  _ok is True and len(_files) == 1,
                  _files[0].name if _files else "nothing written")
            if _files:
                _rec = _js.loads(_files[0].read_text())
                check("...naming the person, the machine and the version",
                      bool(_rec.get("user")) and bool(_rec.get("machine"))
                      and bool(_rec.get("version")))
                _sv = _cfg.INSTALLS_DIR
                _cfg.INSTALLS_DIR = _d
                try:
                    _back = _ms._read_installs()
                finally:
                    _cfg.INSTALLS_DIR = _sv
                check("...and the list reader hands it straight back",
                      len(_back) == 1, f"{len(_back)} record(s)")
        finally:
            _sh.rmtree(_d, ignore_errors=True)

        # An unreachable team folder must SAY the person is invisible. Claiming
        # success here would be the setup-log bug all over again: the
        # reassurance is what stops anybody checking.
        _d = Path(_tf.mkdtemp())
        try:
            _ok, _out = _run(_d, True)
            _flat = " ".join(_out.split())
            check("an unreachable team folder is reported, not glossed over",
                  _ok is False and
                  "nobody else can see that you have this installed" in _flat)
            check("...and nothing is written when it cannot be", not list(_d.glob("*.json")))
        finally:
            _sh.rmtree(_d, ignore_errors=True)

        # A write that fails outright must never claim success, and must never
        # bring setup down -- being invisible to the team does not stop the
        # install working.
        _d = Path(_tf.mkdtemp())
        try:
            (_d / "blocked").write_text("a file where a folder must go")
            _ok, _out = _run(_d / "blocked" / "deeper", False)
            check("a failed write never claims the person is listed",
                  _ok is False and "You are on the list" not in _out)
        finally:
            _sh.rmtree(_d, ignore_errors=True)

        # Registering must not depend on WHICH tool a conversation reaches for.
        # Measured 2026-09-04: a teammate who installed on 2026-09-01 and has
        # the system running was still absent from the list three days later,
        # because check_system_health was the only writer and Claude is merely
        # asked to call it. Setup registering people (above) cannot help a
        # machine that is already installed; this can, on its next question.
        _wrap = _insp.getsource(_ms._with_pending_notice)
        check("the tool wrapper registers this machine too",
              "_write_install_checkin" in _wrap)

        # A stage the machine has already moved past must never be offered.
        # Found by these checks: the development copy had dfe1a2a staged while
        # running 9712632 and the notice offered it -- a DOWNGRADE, the same
        # shape CLAUDE.md records being fixed once in _install_problems.
        _rdy = Path(_cfg.PENDING_UPDATE_DIR) / "ready.json"
        _wasrdy = _rdy.read_bytes() if _rdy.exists() else None
        try:
            _rdy.parent.mkdir(parents=True, exist_ok=True)
            _rdy.write_text(_js.dumps({
                "version": "aaaaaa1",
                "zip_filename": "f.zip",
                "signature": "x",
                "current_version_at_download": "bbbbbb2",
            }))
            check("a stage from before this install moved on is not offered",
                  _ms._update_ready() == "",
                  "staged while on bbbbbb2, now on " + str(_ms._get_code_version()))
            _rdy.write_text(_js.dumps({
                "version": "aaaaaa1",
                "zip_filename": "f.zip",
                "signature": "x",
                "current_version_at_download": _ms._get_code_version(),
            }))
            check("...while a stage from the version still running IS offered",
                  _ms._update_ready() == "aaaaaa1")
        finally:
            if _wasrdy is None:
                if _rdy.exists():
                    _rdy.unlink()
            else:
                _rdy.write_bytes(_wasrdy)

        _d = Path(_tf.mkdtemp())
        _sv = (_cfg.INSTALLS_DIR, _cfg.SHARED_DIR_IS_FALLBACK)
        _st = Path(_cfg.CHECKIN_STAMP_FILE)
        _ust = Path(_cfg.UPDATE_CHECK_STAMP_FILE)
        _wasst = _st.read_bytes() if _st.exists() else None
        _wasust = _ust.read_bytes() if _ust.exists() else None
        try:
            _cfg.INSTALLS_DIR = _d
            _cfg.SHARED_DIR_IS_FALLBACK = False
            # Freeze the update channel so this measures the check-in only.
            _ust.parent.mkdir(parents=True, exist_ok=True)
            _ust.write_text("frozen")
            os.utime(_ust, (_tm.time(), _tm.time()))
            if _st.exists():
                _st.unlink()

            _body2 = "PLAIN ANSWER" * 20
            _got = _ms._with_pending_notice("get_screening_rules", _body2)
            check("an ordinary tool call writes the record",
                  len(list(_d.glob("*.json"))) == 1,
                  f"{len(list(_d.glob('*.json')))} record(s)")
            # startswith, not ==: appending a genuine notice is allowed
            # behaviour here. What must never happen is the answer being
            # changed or lost.
            check("...without altering the answer", _got.startswith(_body2))

            # The daily gate must survive: this now runs on every tool call,
            # and a shared-folder write per call is exactly the 5-second
            # regression this codebase already clawed back once.
            _first = next(_d.glob("*.json")).read_bytes()
            _ms._with_pending_notice("get_screening_rules", _body2)
            check("...and is not rewritten on the very next call",
                  next(_d.glob("*.json")).read_bytes() == _first)

            # And an unusable folder must cost the answer nothing at all.
            _cfg.INSTALLS_DIR = _d / "blocked" / "deeper"
            (_d / "blocked").write_text("a file where a folder must go")
            if _st.exists():
                _st.unlink()
            check("an unusable team folder never costs the answer",
                  _ms._with_pending_notice("get_screening_rules", _body2)
                  .startswith(_body2))
        finally:
            (_cfg.INSTALLS_DIR, _cfg.SHARED_DIR_IS_FALLBACK) = _sv
            if _wasst is None:
                if _st.exists():
                    _st.unlink()
            else:
                _st.write_bytes(_wasst)
            if _wasust is None:
                if _ust.exists():
                    _ust.unlink()
            else:
                _ust.write_bytes(_wasust)
            _sh.rmtree(_d, ignore_errors=True)
    except Exception as e:
        check("install-list registration checks ran", False, f"{type(e).__name__}: {e}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
