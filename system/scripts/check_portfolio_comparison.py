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
        check("a document-verified match is flagged as verified",
              "[verified]" in pc.summarize_match(
                  {"property_name": "X", "plan_type": "subdivide",
                   "outcome_status": "still-held", "notes": "n",
                   "plan_type_source": "documents"}))
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
        _nothing = _m._newer_readable_docs("Zzz No Such Property Zzz", _st)
        check("a property with genuinely no newer documents reports 0, not None",
              _nothing is not None and _nothing[0] == 0, f"got {_nothing!r}")

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
    except Exception as e:
        check("summary currency checks ran", False, f"{type(e).__name__}: {e}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
