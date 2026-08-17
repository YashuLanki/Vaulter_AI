"""
scripts/check_screener.py
-------------------------
Multi-market sanity checks for the CoStar fit screener.

Run it after ANY change to `analysis/screening/fit_screen.py`:

    .venv\\Scripts\\python.exe scripts/check_screener.py [path-to-costar-export]

There is no pytest in this repo, so this is the safety net for the screener the
way `.claude/hooks/check_python_syntax.py` is the safety net for syntax. It runs
no network calls and finishes in seconds.

Why it exists
-------------
The screener is meant to work on any US market, but the only real export
available during development was a 216-row Arizona file. Every bug that mattered
came from a market shape that file did not represent:

  * proximity ranked by how complete the geocoding was, not how good the deals
    were -- invisible in Arizona (20 holdings geocoded), fatal in Texas (4);
  * agricultural parcels found no exit comp at all and dropped out of pricing;
  * a 20-row export produced confident-looking numbers off 2-row peer groups.

So the checks below deliberately deform the one real export into the market
shapes that break things, and assert the screener stays sane. Each check states
what it protects against, and prints PASS/FAIL rather than raising, so one
failure does not hide the rest.

Extended 2026-07-29 (sections 8-13), after a second real export -- the thin
24-column Tucson template -- turned up four more silent wrong answers: a
`Price/Acre` column taken as the total asking price, square footage under a name
with no SF marker taken as acres, residential land use recognised only when
CoStar happened to spell it "Residential", and a sub-acre parcel dropping out of
the pricing test because its lot count rounded to zero. Section 13 screens that
second export directly, and the harness now RUNS on it as a base file too
(`check_screener.py "data/drop/CostarExport (2).xlsx"`) instead of dying on a
KeyError -- checks the base export cannot answer report SKIP and do not count.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

import analysis.screening.fit_screen as fs  # noqa: E402

RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, condition, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def skip(name: str, why: str) -> None:
    """
    A check the BASE EXPORT cannot answer -- not a pass and not a failure.

    Most checks deform the base export into a market shape that breaks things,
    which only works if the base export has the field being deformed. Handed the
    thin 24-column template (no coordinates, size on 5 of 50 rows), a handful of
    checks were asserting things about data that was never there and reporting
    the absence as a screener defect. Skipping says so, and does not count.
    """
    print(f"  SKIP  {name}  — {why}")


def _run(df: pd.DataFrame, holdings, tmp: Path, label: str) -> dict:
    """Screen an in-memory frame with a chosen holdings set."""
    path = tmp / f"{label}.xlsx"
    df.to_excel(path, index=False)
    original = fs.load_holdings
    fs.load_holdings = lambda: holdings
    try:
        return fs.screen(path, write_workbook=False)
    finally:
        fs.load_holdings = original


def main() -> int:
    import logging
    import tempfile

    logging.basicConfig(level=logging.ERROR)

    # Anchored to the project root, not the working directory: this used to be a
    # bare relative path, so it only worked when run from exactly one folder.
    source = (Path(sys.argv[1]) if len(sys.argv) > 1
              else PROJECT_ROOT / "data" / "drop" / "CostarExport.xlsx")
    if not source.exists():
        print(f"No export at {source}. Pass one as an argument.")
        return 2

    src = pd.read_excel(source)
    full = fs.load_holdings()
    tmp = Path(tempfile.mkdtemp())
    print(f"Base export: {source.name} — {len(src)} rows, {len(full)} geocoded holdings\n")

    # ── 1. Proximity must not rank by geocoding completeness ──────────────────
    # The measured bug: with all holdings, 12 listings reached Tier 1; with two
    # holdings, 2 did, and Tier 4 grew from 74 to 158 on identical listings.
    print("1. Proximity is inert when uninformative, never punitive")
    base = _run(src, full, tmp, "prox_full")
    sparse = _run(src, full.head(2), tmp, "prox_sparse")
    none = _run(src, full.iloc[0:0], tmp, "prox_none")

    def tier1(r):
        return sum(v for k, v in r["tier_counts"].items() if k.startswith("1"))

    t_full, t_sparse, t_none = tier1(base), tier1(sparse), tier1(none)
    check("sparse holdings do not collapse the shortlist",
          t_sparse >= t_full * 0.5,
          f"Tier1: {t_full} full -> {t_sparse} with 2 holdings (was 2 before the fix)")
    check("zero holdings does not collapse the shortlist",
          t_none >= t_full * 0.5,
          f"Tier1: {t_full} full -> {t_none} with none")

    # With no holdings every row gets the same proximity score, so the dimension
    # cannot reorder anything. Confirm it truly is constant.
    check("no holdings => proximity is a constant, so ordering is unaffected",
          none["dataframe"]["Cluster_Tier"].nunique() == 1,
          f"tiers present: {list(none['dataframe']['Cluster_Tier'].unique())}")

    # ── 2. Every listing must be priceable, whatever its current use ──────────
    print("\n2. Pricing covers every land type")
    d = base["dataframe"]
    # Only meaningful where the export actually carries sizes and prices. A file
    # with a size on 5 of 50 rows has nothing to build a peer group out of, and
    # "no comparable exit product" is then the correct answer, not a defect.
    # Neither column is guaranteed to exist in the OUTPUT: a file that resolves
    # zero acreage rows (e.g. a template with no Land Area column at all) makes
    # fit_screen.py omit "Land Area (AC)" from `d` entirely rather than filling
    # it with NaN, and this used to crash the harness before a single check ran.
    if "Land Area (AC)" in d.columns and "For Sale Price" in d.columns:
        testable = (d["Land Area (AC)"].notna()
                    & pd.to_numeric(d["For Sale Price"], errors="coerce").notna())
    else:
        testable = pd.Series(False, index=d.index)
    if testable.mean() >= 0.5:
        untestable = d[d["Exit_Comp_N"] == 0]
        check("no listing is left without an exit comp",
              len(untestable) == 0,
              f"{len(untestable)} untestable of {len(d)}")
    else:
        skip("no listing is left without an exit comp",
             f"only {testable.mean():.0%} of this export has both a size and a price")
    # `src` is whatever export was handed in, so nothing here may assume a
    # column exists -- the thin 24-column template has neither Secondary Type
    # nor Land Area, and the harness used to die on a KeyError before running a
    # single check against the very file the bugs surface on.
    for kind in (src["Secondary Type"].dropna().unique()
                 if "Secondary Type" in src.columns else []):
        sub = d[d["Secondary Type"] == kind]
        if len(sub):
            bad = (sub["Exit_Comp_N"] == 0).sum()
            check(f"  {kind} priced", bad == 0, f"{bad} of {len(sub)} untestable")

    # ── 3. Thin and degenerate exports must report low confidence, not guess ──
    print("\n3. Degenerate exports degrade honestly")
    tiny = _run(src.sample(min(20, len(src)), random_state=7), full, tmp, "tiny")
    conf = tiny["dataframe"]["Pricing_Confidence"].value_counts().to_dict()
    check("a 20-row export admits low confidence",
          conf.get("low", 0) + conf.get("medium", 0) > 0,
          f"confidence mix: {conf}")

    priceless = src.copy()
    priceless["For Sale Price"] = None
    pr = _run(priceless, full, tmp, "nopricing")
    check("an export with no prices marks every row untestable",
          (pr["dataframe"]["Exit_Comp_N"] == 0).all()
          or (pr["dataframe"]["Exit_Headroom"].isna()).all(),
          "no fabricated headroom")

    # CoStar ships more than one export template. A thin one omitting Land Area,
    # Secondary Type, coordinates and days-on-market gives the screener nothing
    # to rank on -- and percentile tiering then put ALL 50 rows of a real Tucson
    # export into "1 - Pursue", because equal scores all rank joint-first. A file
    # the screener learned nothing from must not read as fifty deals to chase.
    thin = src.copy()
    for c in ("Land Area (AC)", "Secondary Type", "Latitude", "Longitude", "Days On Market"):
        if c in thin.columns:
            thin = thin.drop(columns=[c])
    th = _run(thin, full, tmp, "thintemplate")
    tiers = th["dataframe"]["Fit_Tier"].value_counts().to_dict()
    top = sum(n for t, n in tiers.items() if "Pursue" in t)
    check("a thin template does not put most of the file in the top tier",
          top <= len(thin) * 0.25, f"{top} of {len(thin)} in Pursue — {tiers}")
    check("  ...and still returns every listing",
          len(th["dataframe"]) == len(thin), f"{len(th['dataframe'])} of {len(thin)}")

    # A list with nothing but locations genuinely cannot be ranked. Tiering it
    # anyway sent every row to the top, because percentile rank makes an
    # all-tied file joint-first for everyone.
    # Synthetic, because the real export's addresses include things like
    # "Gila Bend 160 Acres South" -- the resolver correctly pulls a size out of
    # those, so a subset of the real file is not actually featureless.
    flat = pd.DataFrame({
        "Property Address": [f"{n} Main St" for n in range(1, 31)],
        "City": ["Somewhere"] * 30, "State": ["AZ"] * 30,
        "Market Name": ["Somewhere, AZ"] * 30, "County Name": ["Nowhere"] * 30,
    })
    fl = _run(flat, full, tmp, "flat")
    ftiers = fl["dataframe"]["Fit_Tier"].value_counts().to_dict()
    check("an address-only list says 'unranked' instead of inventing tiers",
          all("Unranked" in t for t in ftiers), f"{ftiers}")

    # ── 3b. Column resolution: no two exports carry the same columns ──────────
    # A real Tucson export (2026-07-28) arrived with 24 columns and none of the
    # names the screener read. These assert the concepts are found under other
    # names and derived where possible, rather than the file being written off.
    print("\n3b. Columns are resolved from whatever the export provides")

    renamed = src.rename(columns={"Land Area (AC)": "Acreage",
                                  "For Sale Price": "Asking Price",
                                  "Days On Market": "DOM"})
    rn = _run(renamed, full, tmp, "renamed")
    check("differently-named columns are still found",
          rn["dataframe"]["Exit_Headroom"].notna().sum()
          >= base["dataframe"]["Exit_Headroom"].notna().sum() * 0.95,
          f"{rn['dataframe']['Exit_Headroom'].notna().sum()} priced vs "
          f"{base['dataframe']['Exit_Headroom'].notna().sum()} in the original")

    if "Land Area (SF)" in src.columns:
        sf_only = src.drop(columns=["Land Area (AC)"])
        sf = _run(sf_only, full, tmp, "sfonly")
        got = {c["field"]: c for c in sf["column_sources"]}["Land Area (AC)"]
        check("acreage converts from a square-footage column",
              got["rows"] > 0 and "square feet" in got["derived"],
              f"{got['rows']} rows, {got['derived'] or 'NOT derived'}")

    if "Secondary Type" in src.columns:
        no_type = src.drop(columns=["Secondary Type"])
        nt = _run(no_type, full, tmp, "notype")
        got = {c["field"]: c for c in nt["column_sources"]}["Secondary Type"]
        check("land type falls back to Proposed Land Use, not the constant 'Land'",
              got["rows"] > 0 and got["source"] != "Property Type",
              f"resolved from '{got['source']}' on {got['rows']} rows")

    titles = pd.DataFrame({
        "Property Name": ["RARE! 11.77 acres NW corner", "±73.55 acres at NWC Moore Rd",
                          "60 acres", "no size mentioned here"],
        "Property Address": ["1 A St", "2 B St", "3 C St", "4 D St"],
        "City": ["Tucson"] * 4, "State": ["AZ"] * 4,
        "Market Name": ["Tucson, AZ"] * 4, "County Name": ["Pima"] * 4,
        "For Sale Price": [1e6, 2e6, 3e6, 4e6],
    })
    tt = _run(titles, full, tmp, "titles")
    got = {c["field"]: c for c in tt["column_sources"]}["Land Area (AC)"]
    check("acreage written into a listing title is recovered",
          got["rows"] == 3, f"{got['rows']} of 4 rows (the 4th states no size)")

    # Names nobody wrote an alias for. The screener must recognise the concept
    # from the name's shape plus the values, not from a fixed list.
    unseen = src.rename(columns={"Land Area (AC)": "Gross Site Area",
                                 "For Sale Price": "Ask",
                                 "Latitude": "Y Coord", "Longitude": "X Coord"})
    un = _run(unseen, full, tmp, "unseen")
    g = {c["field"]: c for c in un["column_sources"]}
    # Only the concepts the base export actually supplies can be renamed and
    # then re-found. The thin template has no coordinates to rename.
    renamable = [f for f, orig in (("Land Area (AC)", "Land Area (AC)"),
                                   ("For Sale Price", "For Sale Price"),
                                   ("Latitude", "Latitude"))
                 if orig in src.columns and src[orig].notna().any()]
    if renamable:
        check("column names never seen before are matched on shape and values",
              all(g[f]["rows"] > 0 for f in renamable),
              "Gross Site Area / Ask / Y Coord -> " +
              ", ".join(f"{f}={g[f]['rows']}" for f in renamable))
    else:
        skip("column names never seen before are matched on shape and values",
             "this export supplies none of the three concepts to rename")

    # THE dangerous one. "Land Area (SF)" matches the land+area name pattern and
    # 1.7 million square feet passed a loose range check, so a 40-acre parcel was
    # silently read as 1.7 million acres. Square footage must convert, never be
    # taken at face value -- and converting must reproduce the original exactly.
    if {"Land Area (AC)", "Land Area (SF)"} <= set(src.columns):
        sf_only = src.drop(columns=["Land Area (AC)"])
        so = _run(sf_only, full, tmp, "sfonly2")
        g = {c["field"]: c for c in so["column_sources"]}["Land Area (AC)"]
        check("square footage is converted, never mistaken for acres",
              "square feet" in g["derived"] and so["tier_counts"] == base["tier_counts"],
              f"from '{g['source']}' via {g['derived'] or 'NOTHING'}; "
              f"tiers {'match' if so['tier_counts'] == base['tier_counts'] else 'DIFFER'}")

    # ── 3c. The header is not always the first row ────────────────────────────
    print("\n3c. A title block above the header does not break the read")
    xl_path = tmp / "titleblock.xlsx"
    with pd.ExcelWriter(xl_path, engine="openpyxl") as xl:
        pd.DataFrame([["ACME REALTY — Land Opportunities"], ["Prepared 28 July 2026"], []]) \
            .to_excel(xl, sheet_name="Sheet1", index=False, header=False)
        src.to_excel(xl, sheet_name="Sheet1", index=False, startrow=3)
    xr = fs.screen(xl_path, write_workbook=False)
    check("an xlsx with 3 junk rows above the header reads identically",
          xr["tier_counts"] == base["tier_counts"],
          f"{xr['total_screened']} rows, tiers "
          f"{'match' if xr['tier_counts'] == base['tier_counts'] else xr['tier_counts']}")

    csv_path = tmp / "titleblock.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        fh.write("ACME REALTY - Land Opportunities\nPrepared 28 July 2026\n\n")
        src.to_csv(fh, index=False)
    cr = fs.screen(csv_path, write_workbook=False)
    check("a CSV with a title block reads identically (pandas alone throws here)",
          cr["tier_counts"] == base["tier_counts"],
          f"{cr['total_screened']} rows, tiers "
          f"{'match' if cr['tier_counts'] == base['tier_counts'] else cr['tier_counts']}")

    # ── 4. A genuinely different market must screen without special-casing ────
    print("\n4. Other markets")
    tx = src.copy()
    tx["State"] = "TX"
    tx["Market Name"] = "Dallas-Fort Worth"
    tx["County Name"] = "Kaufman"
    tx["Submarket Name"] = "Forney"
    tx_res = _run(tx, full[full.state == "TX"], tmp, "texas")
    # These two need the underlying export to actually carry a size and a
    # price on enough rows to produce a shortlist at all -- relabeling the
    # state doesn't manufacture data that isn't there. Same `testable` guard
    # as section 2, so a thin file skips honestly instead of failing on a
    # claim its own data can't support either way.
    if testable.mean() >= 0.5:
        check("a single-market Texas export produces a shortlist",
              tier1(tx_res) > 0, f"Tier1={tier1(tx_res)}")
    else:
        skip("a single-market Texas export produces a shortlist",
             f"only {testable.mean():.0%} of this export has both a size and a price")

    mixed = src.copy()
    third = len(mixed) // 3
    mixed.loc[mixed.index[:third], ["State", "Market Name", "County Name"]] = ["TX", "Austin", "Travis"]
    mixed.loc[mixed.index[third:2 * third], ["State", "Market Name", "County Name"]] = ["CO", "Denver", "Weld"]
    mx = _run(mixed, full, tmp, "mixed")
    check("a three-state export screens without error",
          mx["total_screened"] == len(mixed), f"{mx['total_screened']} rows")
    if testable.mean() >= 0.5:
        check("peer groups stay within a market",
              mx["dataframe"]["Exit_Comp_Basis"].nunique() > 1,
              f"{mx['dataframe']['Exit_Comp_Basis'].nunique()} distinct comp bases")
    else:
        skip("peer groups stay within a market",
             f"only {testable.mean():.0%} of this export has both a size and a price")

    if "Secondary Type" in src.columns and src["Secondary Type"].notna().any():
        single_type = src[src["Secondary Type"] == src["Secondary Type"].mode()[0]]
        st = _run(single_type, full, tmp, "singletype")
        check("a single-land-type export screens",
              st["total_screened"] == len(single_type), f"{len(single_type)} rows")

    # ── 5. Nothing may ever be eliminated ─────────────────────────────────────
    print("\n5. The screener ranks, it never filters")
    for label, r in (("full", base), ("texas", tx_res), ("mixed", mx), ("tiny", tiny)):
        n_in = len(pd.read_excel(tmp / f"{ {'full':'prox_full','texas':'texas','mixed':'mixed','tiny':'tiny'}[label] }.xlsx"))
        check(f"  {label}: every row survives", len(r["dataframe"]) == n_in,
              f"{len(r['dataframe'])} out of {n_in}")

    # ── 6. Costs are measured, and absent costs are declared ──────────────────
    # Added 2026-07-28 when cost_load (an invented 35% of purchase price) was
    # replaced with entitlement measured per lot from three Arizona budgets.
    # The failure these guard against is a cost silently reverting to a single
    # flat number, or a type with no cost record being priced as if it had one.
    print("\n6. Cost model is measured, and silence about a cost is stated")
    bd = base["dataframe"]

    # The measured $/lot figures live in the gitignored cost_assumptions.json,
    # which deliberately never ships -- so on every teammate's install there is
    # no entitlement cost to assert about, and these checks are about the shape
    # of a cost curve that isn't there. Skipped rather than failed: a fresh
    # install reporting six FAILs reads as "this product is broken" when it is
    # working exactly as designed. Found 2026-08-12 on a genuine fresh install,
    # which is also the first time this suite had ever RUN in the configuration
    # everyone other than the maintainer actually has.
    HAS_COST = bool(fs.ASSUMPTIONS.get("entitlement_per_lot_anchors"))

    if HAS_COST:
        lo, hi = fs._entitlement_per_lot(48), fs._entitlement_per_lot(220)
        mid = fs._entitlement_per_lot(116)
        check("entitlement per lot falls as projects get bigger",
              lo > mid > hi, f"48 lots ${lo:,.0f} > 116 ${mid:,.0f} > 220 ${hi:,.0f}")
        check("entitlement per lot is flat outside the measured range, never extrapolated",
              fs._entitlement_per_lot(5) == lo and fs._entitlement_per_lot(5000) == hi,
              f"5 lots ${fs._entitlement_per_lot(5):,.0f}, "
              f"5000 lots ${fs._entitlement_per_lot(5000):,.0f}")
    else:
        skip("entitlement per lot falls as projects get bigger",
             "no local cost_assumptions.json -- expected on any install but the maintainer's")
        skip("entitlement per lot is flat outside the measured range, never extrapolated",
             "no local cost_assumptions.json")

    # Split with the SAME pattern the screener uses. Selecting on the literal
    # word "Residential" here put "Single Family Development" and "Apartment
    # Units" rows into `other` and then asserted they were labelled as having no
    # cost record -- the harness would have agreed with the bug it exists to
    # catch. Only visible on an export whose land use is worded CoStar's way.
    resi = bd[bd["Secondary Type"].astype(str).str.contains(
        fs._RESIDENTIAL_PAT, case=False, regex=True, na=False)]
    other = bd[~bd.index.isin(resi.index)]
    # Entitlement is priced per lot, so it needs an acreage to derive a lot
    # count from. A residential row with no size correctly carries none.
    # Guard the column's existence too: an export that resolves zero acreage
    # rows anywhere (e.g. no Land Area column at all) makes fit_screen.py omit
    # "Land Area (AC)" from the output entirely, not just leave it all-NaN.
    resi_sized = (resi[resi["Land Area (AC)"].notna()]
                  if "Land Area (AC)" in resi.columns else resi.iloc[0:0])
    if not HAS_COST:
        skip("residential rows carry an entitlement cost",
             "no local cost_assumptions.json -- nothing to price them from")
        # The rule that still MUST hold without the cost file: every row says
        # so. This is the honesty guarantee ("a cost with no record is declared,
        # never estimated") and it is the one thing a teammate's screen depends
        # on being right, so it is asserted here rather than skipped.
        check("with no cost data, EVERY row declares the exit is understated",
              bd["Cost_Basis"].astype(str).str.contains("understated").all(),
              f"{bd['Cost_Basis'].astype(str).str.contains('understated').sum()} "
              f"of {len(bd)} rows declared")
    elif len(resi_sized):
        check("residential rows carry an entitlement cost",
              resi_sized["Entitlement_Per_Acre"].notna().all()
              and (resi_sized["Entitlement_Per_Acre"] > 0).all(),
              f"{len(resi_sized)} rows, ${resi_sized['Entitlement_Per_Acre'].min():,.0f}–"
              f"${resi_sized['Entitlement_Per_Acre'].max():,.0f}/ac")
    else:
        skip("residential rows carry an entitlement cost",
             f"none of this export's {len(resi)} residential rows has an acreage")
    check("rows with no entitlement record say the required exit is understated",
          other["Cost_Basis"].str.contains("understated").all(),
          f"{len(other)} non-residential rows labelled")
    check("carry is charged over the observed hold, not the underwritten one",
          fs.ASSUMPTIONS["hold_years_actual"] > fs.ASSUMPTIONS["hold_years_underwritten"]
          and (bd["Carry_Per_Acre"].dropna() > 0).all(),
          f"{fs.ASSUMPTIONS['carry_rate_annual']:.2%}/yr over "
          f"{fs.ASSUMPTIONS['hold_years_actual']}yr")
    # The real $/acre figures live in the gitignored cost_assumptions.json and
    # deliberately never ship, so on ANY teammate's install they are None. This
    # assertion used to compare None > 0 and crash the whole suite there --
    # meaning the checks had never once run in the configuration everyone else
    # actually has. Found 2026-08-12 on a genuine fresh install. What matters is
    # the same either way: horizontal cost must stay out of the arithmetic.
    _h_lo = fs.ASSUMPTIONS.get("horizontal_per_acre_low")
    _h_hi = fs.ASSUMPTIONS.get("horizontal_per_acre_high")
    check("horizontal cost stays OUT of the arithmetic (Pinal-only evidence)",
          "horizontal_cost_per_acre" not in fs.ASSUMPTIONS
          and (_h_lo is None or _h_lo > 0),
          "context only; "
          + (f"${_h_lo:,}–${_h_hi:,}/ac" if _h_lo is not None
             else "no figures on this machine (private cost file absent, as expected)"))
    check("lot yield matches the measured range, not the old 8.0",
          (fs.ASSUMPTIONS["lots_per_acre_low"] <= fs.ASSUMPTIONS["lots_per_acre"]
           <= fs.ASSUMPTIONS["lots_per_acre_high"]),
          f"{fs.ASSUMPTIONS['lots_per_acre']} within "
          f"{fs.ASSUMPTIONS['lots_per_acre_low']}–{fs.ASSUMPTIONS['lots_per_acre_high']}")

    # ── 7. The screen states what the portfolio can't tell you ────────────────
    # A market with no history must still rank normally AND say it has no
    # history. Reporting Arizona as unevidenced (a state-abbreviation bug caught
    # on the first run) is the most misleading output this can produce.
    print("\n7. Evidence coverage is reported per market")
    az = {c["state"]: c for c in base["evidence_coverage"]}
    # This check's premise is that the file under test is itself Arizona --
    # true for the default baseline and for CostarExport (2).xlsx (Tucson is
    # Arizona too), but not for an arbitrary file someone points the harness
    # at directly. Skip rather than fail when that premise doesn't hold.
    has_az = "State" in src.columns and src["State"].astype(str).str.upper().eq("AZ").any()
    if has_az:
        check("Arizona is reported as fully evidenced",
              "Arizona" in az and az["Arizona"]["evidence"].startswith("full"),
              f"{list(az)} -> {az.get('Arizona', {}).get('evidence', 'MISSING')[:40]}")
    else:
        skip("Arizona is reported as fully evidenced",
             "this export's own State column isn't Arizona -- nothing to check")
    check("state abbreviations resolve to full names",
          fs._state_name("AZ") == "Arizona" and fs._state_name("Colorado") == "Colorado",
          "AZ -> Arizona, Colorado -> Colorado")

    for st, name in (("TX", "Texas"), ("CO", "Colorado")):
        d = src.copy()
        d["State"] = st
        r = _run(d, full, tmp, f"cov{st}")
        cov = {c["state"]: c for c in r["evidence_coverage"]}
        t1 = sum(v for k, v in r["tier_counts"].items() if k.startswith("1"))
        # "still ranked" (t1 > 0) needs the same size+price data as above --
        # relabeling the state doesn't manufacture a shortlist from nothing.
        if testable.mean() >= 0.5:
            check(f"  {name} is declared unevidenced but still ranked",
                  name in cov and cov[name]["evidence"].startswith("none") and t1 > 0,
                  f"Tier1={t1}, evidence='{cov.get(name, {}).get('evidence', 'MISSING')[:28]}...'")
        else:
            skip(f"  {name} is declared unevidenced but still ranked",
                 f"only {testable.mean():.0%} of this export has both a size and a price")

    check("every listing gets a plain-language reason",
          bd["Why"].notna().all() and (bd["Why"].str.len() > 20).all(),
          f"shortest {bd['Why'].str.len().min()} chars")

    # ── 8. A gap is named, never blurred into a neighbouring gap ──────────────
    # Added 2026-07-29. "No price or no comparable exit product" was one
    # sentence for four different situations, and printing "no asking price"
    # next to a visible asking price reads as a bug rather than as a finding.
    # Each cause needs a different response from the reader -- re-export with
    # the column, or accept that this file cannot answer it.
    print("\n8. Untestable rows say WHICH thing is missing")

    filler = pd.DataFrame({
        "Property Address": [f"{n} Filler Rd" for n in range(1, 13)],
        "City": ["Casa Grande"] * 12, "State": ["AZ"] * 12,
        "Market Name": ["Phoenix"] * 12, "County Name": ["Pinal"] * 12,
        "Submarket Name": ["Casa Grande"] * 12,
        "Secondary Type": ["Commercial"] * 12,
        "Land Area (AC)": [4, 6, 8, 9, 11, 12, 13, 14, 15, 16, 17, 18],
        "For Sale Price": [8e5, 9e5, 1e6, 1.1e6, 1.2e6, 1.3e6,
                           1.4e6, 1.5e6, 1.6e6, 1.7e6, 1.8e6, 1.9e6],
    })
    gaps = pd.DataFrame({
        "Property Address": ["A priced+sized", "B no size", "C no price",
                             "D neither", "E no comp"],
        "City": ["Casa Grande"] * 5, "State": ["AZ"] * 5,
        "Market Name": ["Phoenix"] * 5, "County Name": ["Pinal"] * 5,
        "Submarket Name": ["Casa Grande"] * 5,
        "Secondary Type": ["Commercial", "Commercial", "Commercial",
                           "Commercial", "Sod Farm Nursery"],
        "Land Area (AC)": [30, None, 30, None, 30],
        "For Sale Price": [3e6, 3e6, None, None, 3e6],
    })
    gp = _run(pd.concat([gaps, filler], ignore_index=True), full, tmp, "gaps")
    g = gp["dataframe"].set_index("Property Address")

    def _verdict(addr):
        return str(g.loc[addr, "Pricing_Verdict"]).lower()

    check("a priced listing with no comparable does not claim it has no price",
          "no comparable exit product" in _verdict("E no comp")
          and "asking price" not in _verdict("E no comp"),
          _verdict("E no comp")[:70])
    check("a listing with no asking price says exactly that",
          _verdict("C no price").startswith("no asking price"),
          _verdict("C no price")[:70])
    check("a listing with no parcel size says the SIZE is what is missing",
          "no parcel size" in _verdict("B no size")
          and "no asking price" not in _verdict("B no size"),
          _verdict("B no size")[:70])
    check("a listing missing both says both",
          "neither an asking price nor a parcel size" in _verdict("D neither"),
          _verdict("D neither")[:70])
    check("a listing with a price, a size and a comp is still testable",
          pd.notna(g.loc["A priced+sized", "Exit_Headroom"]),
          f"headroom {g.loc['A priced+sized', 'Exit_Headroom']}")

    # The one-line Why and the Pricing_Verdict must not disagree about which
    # field is missing -- they are read side by side in the MCP output.
    gd = gp["dataframe"]
    untest = gd[gd["Exit_Headroom"].isna()]
    agree = all(str(r["Why"]).lower().startswith(str(r["Pricing_Verdict"]).split(" — return multiple")[0].lower())
                for _, r in untest.iterrows())
    check("  ...and Why agrees with Pricing_Verdict about which field is missing",
          agree and len(untest) == 4, f"{len(untest)} untestable rows checked")

    # No untestable row may carry a caveat about a number that was never
    # computed. "no asking price; entitlement cost not included" was real.
    check("no caveat about the required exit on a row that has no required exit",
          not untest["Why"].str.contains("entitlement cost not included").any(),
          "the understated-exit caveat is suppressed where there is no headroom")

    # ── 9. A blank land type is named, not left as a hole in the line ─────────
    # An empty gap in a summary line reads as a formatting fault. 9 of 50 rows
    # on the real Tucson export have no Proposed Land Use.
    print("\n9. A missing land type says Unknown")
    blank_type = filler.copy()
    blank_type.loc[blank_type.index[:4], "Secondary Type"] = ["", None, "  ", float("nan")]
    bt = _run(blank_type, full, tmp, "blanktype")["dataframe"]["Secondary Type"].astype(str)
    check("blank land types render as 'Unknown', never as an empty gap",
          (bt.str.strip() != "").all() and not bt.isin(["nan", "None", "NaN"]).any()
          and (bt == "Unknown").sum() == 4,
          f"{(bt == 'Unknown').sum()} of 4 blanks named; values {sorted(set(bt))}")

    notype_at_all = filler.drop(columns=["Secondary Type"])
    na = _run(notype_at_all, full, tmp, "notypecol")["dataframe"]
    check("an export with no land-type column at all still prints a type",
          "Secondary Type" in na.columns
          and (na["Secondary Type"].astype(str).str.strip() != "").all(),
          f"all rows read '{na['Secondary Type'].iloc[0]}'")

    # ── 10. Residential is recognised however the export words it ─────────────
    # The measured entitlement budgets are all residential subdivisions. Matching
    # only the literal word "Residential" meant 0 of 50 rows on the Tucson export
    # carried an entitlement cost, and each was labelled "no entitlement cost on
    # record for non-residential" -- a false statement about the firm's budgets.
    print("\n10. Residential is recognised under CoStar's other names for it")
    wordings = ["Residential", "Single Family Development", "Single Family Residence",
                "Apartment Units", "MultiFamily", "Condo", "Townhome"]
    resi_df = pd.DataFrame({
        "Property Address": [f"{n} Resi Way" for n in range(len(wordings))],
        "City": ["Casa Grande"] * len(wordings), "State": ["AZ"] * len(wordings),
        "Market Name": ["Phoenix"] * len(wordings), "County Name": ["Pinal"] * len(wordings),
        "Submarket Name": ["Casa Grande"] * len(wordings),
        "Secondary Type": wordings,
        "Land Area (AC)": [40] * len(wordings),
        "For Sale Price": [2e6] * len(wordings),
    })
    rw = _run(pd.concat([resi_df, filler], ignore_index=True), full, tmp, "resiwords")["dataframe"]
    rw = rw[rw["Secondary Type"].isin(wordings)]
    if HAS_COST:
        check("every CoStar wording of residential carries an entitlement cost",
              rw["Entitlement_Per_Acre"].notna().all() and (rw["Entitlement_Per_Acre"] > 0).all(),
              f"{len(rw)} wordings, all priced" if rw["Entitlement_Per_Acre"].notna().all()
              else f"missing on {list(rw[rw['Entitlement_Per_Acre'].isna()]['Secondary Type'])}")
        check("  ...and none of them is told there is no cost record for its type",
              not rw["Cost_Basis"].str.contains("understated").any(),
              f"{rw['Cost_Basis'].str.contains('understated').sum()} mislabelled")
    else:
        skip("every CoStar wording of residential carries an entitlement cost",
             "no local cost_assumptions.json")
        # What must still hold: each wording is RECOGNISED as residential, which
        # is what routes it to a lot-based exit. That is independent of whether
        # a cost figure exists, so it stays a real assertion.
        check("every CoStar wording of residential is still recognised as residential",
              rw["Vaulter_Read"].str.contains("exits as lots", case=False).all(),
              f"{rw['Vaulter_Read'].str.contains('exits as lots', case=False).sum()} of {len(rw)}")
    check("  ...and the exit-path note says lots, not 'depends what it is entitled for'",
          rw["Vaulter_Read"].str.contains("exits as lots", case=False).all(),
          f"{rw['Vaulter_Read'].str.contains('exits as lots', case=False).sum()} of {len(rw)}")

    # ── 11. A small parcel must not fall out of the pricing test ──────────────
    # Under 0.29 acres the indicative lot count rounded to ZERO, entitlement per
    # lot returned NaN, and the NaN reached Exit_Headroom -- so a small parcel
    # WITH a price and WITH a comp came back "return multiple untestable".
    print("\n11. Small parcels stay testable")
    small = pd.DataFrame({
        "Property Address": ["Tiny 0.10ac", "Tiny 0.25ac", "Small 2ac"],
        "City": ["Casa Grande"] * 3, "State": ["AZ"] * 3,
        "Market Name": ["Phoenix"] * 3, "County Name": ["Pinal"] * 3,
        "Submarket Name": ["Casa Grande"] * 3,
        "Secondary Type": ["Residential"] * 3,
        "Land Area (AC)": [0.10, 0.25, 2.0],
        "For Sale Price": [60_000, 120_000, 700_000],
    })
    sm_src = pd.concat([small, filler.assign(**{"Secondary Type": "Residential"})],
                       ignore_index=True)
    sm = _run(sm_src, full, tmp, "smallparcels")["dataframe"]
    sm = sm[sm["Property Address"].str.startswith(("Tiny", "Small"))]
    if HAS_COST:
        check("a sub-acre residential parcel with a price and a comp gets a headroom",
              sm["Exit_Headroom"].notna().all(),
              f"{sm['Exit_Headroom'].notna().sum()} of {len(sm)} priced — "
              f"{list(sm['Exit_Headroom'])}")
    else:
        skip("a sub-acre residential parcel with a price and a comp gets a headroom",
             "no local cost_assumptions.json -- headroom needs an entitlement cost")
    check("  ...and is credited with at least one lot, never zero",
          (sm["Indicative_Lots"] >= 1).all(),
          f"lots {list(sm['Indicative_Lots'])}")

    # Expected values come from the real, gitignored cost_assumptions.json --
    # never hardcoded here, since that file is the single source of truth and
    # this is a public, tracked test file. Absent that file, these two checks
    # are skipped rather than asserting against a stale or fabricated number.
    anchors = fs.ASSUMPTIONS.get("entitlement_per_lot_anchors")
    if anchors:
        sweep = [fs._entitlement_per_lot(n) for n in range(1, 400)]
        expected_max = max(y for _, y in anchors)
        expected_min = min(y for _, y in anchors)
        check("entitlement per lot never rises with project size, and stays inside the anchors",
              all(a >= b for a, b in zip(sweep, sweep[1:]))
              and max(sweep) == expected_max and min(sweep) == expected_min,
              f"1 lot ${sweep[0]:,.0f} -> 399 lots ${sweep[-1]:,.0f}, monotone")
        check("a zero or negative lot count does not poison the arithmetic with NaN",
              fs._entitlement_per_lot(0) != fs._entitlement_per_lot(0)
              and fs._entitlement_per_lot(48) == anchors[0][1],
              "0 lots -> NaN (and add_pricing floors the count at 1 before asking)")
    else:
        print("  SKIP  entitlement-per-lot checks -- no local cost_assumptions.json present")

    # ── 12. A column that means something else must not be taken for it ───────
    # `_norm` turns punctuation into spaces before the avoid patterns run, so
    # `/\s*(sf|ac|unit)` could never match: "Price/Acre" normalised to
    # "price acre" and won the asking-price slot with a real, plausible-looking
    # per-acre price.
    print("\n12. Per-unit and wrong-unit columns are refused, not guessed at")
    # Synthetic, and deliberately independent of whichever export was handed in:
    # the point is the column NAME and the magnitude of its values, not the
    # market. Half-acre pads are the case that matters, because their square
    # footage lands in exactly the range the old ceiling let through.
    pads = pd.DataFrame({
        "Property Address": [f"{n} Pad Ct" for n in range(1, 13)],
        "City": ["Casa Grande"] * 12, "State": ["AZ"] * 12,
        "Market Name": ["Phoenix"] * 12, "County Name": ["Pinal"] * 12,
        "Submarket Name": ["Casa Grande"] * 12,
        "Secondary Type": ["Commercial"] * 12,
        "Land Area (AC)": [0.40, 0.45, 0.50, 0.55, 0.60, 0.65,
                           0.70, 0.75, 0.80, 0.85, 0.90, 0.95],
        "For Sale Price": [2e5, 2.2e5, 2.5e5, 2.8e5, 3e5, 3.2e5,
                           3.5e5, 3.8e5, 4e5, 4.2e5, 4.5e5, 5e5],
    })

    for colname in ("Price/Acre", "Price/SF", "Price Per Acre", "$/AC"):
        perunit = filler.drop(columns=["For Sale Price"]).copy()
        perunit[colname] = (filler["For Sale Price"] / filler["Land Area (AC)"]).round(0)
        pu = _run(perunit, full, tmp, f"perunit{abs(hash(colname)) % 9999}")
        got = {c["field"]: c for c in pu["column_sources"]}["For Sale Price"]
        check(f"  a '{colname}' column is not taken as the asking price",
              got["source"] != colname,
              f"median {perunit[colname].median():,.0f} -> price slot "
              f"'{got['source'] or 'nothing (correct — the rows abstain)'}'")

    # Square feet under a name with no SF marker, so the `avoid` list cannot see
    # it. Half-acre pads have a median around 28,000 sq ft, which sailed under
    # the old 100,000-acre ceiling -- every one of them read as 28,000 acres.
    sf_unmarked = pads.drop(columns=["Land Area (AC)"]).copy()
    sf_unmarked["Lot Size"] = (pads["Land Area (AC)"] * 43560.0).round(0)
    su = _run(sf_unmarked, full, tmp, "sfunmarked")
    got = {c["field"]: c for c in su["column_sources"]}["Land Area (AC)"]
    check("square feet under a name with no SF marker is refused, not read as acres",
          got["source"] != "Lot Size",
          f"median {sf_unmarked['Lot Size'].median():,.0f} -> acreage slot "
          f"'{got['source'] or 'nothing (correct — the rows abstain)'}'")

    # ...and the ceiling must not be so tight that real acreage is refused.
    ac_named = pads.drop(columns=["Land Area (AC)"]).copy()
    ac_named["Lot Size"] = pads["Land Area (AC)"]
    an = _run(ac_named, full, tmp, "acnamed")
    got = {c["field"]: c for c in an["column_sources"]}["Land Area (AC)"]
    check("  ...but a 'Lot Size' column that really is acres is still accepted",
          got["source"] == "Lot Size" and got["rows"] == len(pads),
          f"'{got['source']}' on {got['rows']} rows (median "
          f"{ac_named['Lot Size'].median():,.2f} ac)")

    big = ac_named.copy()
    big["Lot Size"] = pads["Land Area (AC)"] * 6000        # median ~3,900 acres
    bg = _run(big, full, tmp, "bigacres")
    got = {c["field"]: c for c in bg["column_sources"]}["Land Area (AC)"]
    check("  ...and a genuine ranch-scale export is not refused either",
          got["source"] == "Lot Size",
          f"median {big['Lot Size'].median():,.0f} ac -> '{got['source']}'")

    # ── 13. The second real export, in full ───────────────────────────────────
    # Every recent bug surfaced on the thin 24-column Tucson template rather
    # than on the 216-row Phoenix file. Screen it if it is on this machine.
    thin_real = source.parent / "CostarExport (2).xlsx"
    if thin_real.exists():
        print(f"\n13. The second real export ({thin_real.name})")
        tr = fs.screen(thin_real, write_workbook=False)
        td = tr["dataframe"]
        priced = td[pd.to_numeric(td["For Sale Price"], errors="coerce").notna()]
        check("no listing with a visible asking price is told it has none",
              not priced["Why"].str.contains("no asking price", case=False).any(),
              f"{len(priced)} priced rows of {len(td)}")
        check("no summary line would render a blank land type",
              (td["Secondary Type"].astype(str).str.strip() != "").all(),
              f"{(td['Secondary Type'] == 'Unknown').sum()} rows read 'Unknown'")
        resi_words = td["Secondary Type"].astype(str).str.contains(
            "Single Family|Apartment|MultiFamily|Residential", case=False, na=False)
        check("residential rows are not labelled 'no cost record for non-residential'",
              not td[resi_words]["Cost_Basis"].str.contains("understated").any(),
              f"{int(resi_words.sum())} residential rows found by wording")
        check("  ...and every row still comes back",
              tr["total_screened"] == len(pd.read_excel(thin_real)),
              f"{tr['total_screened']} rows, tiers {tr['tier_counts']}")

    # ── 14. Portfolio comparison never crashes the screen, never fabricates ──
    # Added 2026-08-06 when compare_to_portfolio_history was wired into every
    # row. Its own regression suite (check_portfolio_comparison.py) covers the
    # scoring logic; this just confirms the column shows up correctly on a
    # real export and never silently disappears or errors out mid-screen.
    print("\n14. Portfolio comparison column")
    check("Portfolio_Comparison column exists on the base export",
          "Portfolio_Comparison" in base["dataframe"].columns)
    pc = base["dataframe"]["Portfolio_Comparison"]
    check("every row has a value (a real comparison or an honest empty string, never null)",
          pc.notna().all(), f"{pc.isna().sum()} null rows")
    check("no comparison text mentions a price -- this only compares characteristics/history",
          not pc.astype(str).str.contains(r"\$", regex=True).any(),
          "a dollar sign appearing here would mean price leaked into a tool that must not compare it")

    # ── 15. Passed-on-deal patterns never touch score/rank ──────────────────
    # Added 2026-08-06 with passed_on_patterns.py. The one rule that must
    # never break: a documented "we passed on deals like this before" caution
    # is informational only. Deform two identical rows so only their county
    # differs, and confirm the pattern fires on the matching one, stays
    # silent on the other, and neither row's score moves because of it.
    print("\n15. Passed-on-deal patterns are informational only")
    weld = src.copy()
    weld["State"] = "CO"
    weld["County Name"] = "Weld"
    other = src.copy()
    other["State"] = "CO"
    other["County Name"] = "Larimer"
    r_weld = _run(weld, full, tmp, "weld")
    r_other = _run(other, full, tmp, "larimer")
    d_weld, d_other = r_weld["dataframe"], r_other["dataframe"]
    check("Weld County rows get the documented oil & gas caution",
          d_weld["Cautions"].str.contains("oil & gas", case=False, na=False).all(),
          f"{d_weld['Cautions'].str.contains('oil & gas', case=False, na=False).sum()} of "
          f"{len(d_weld)} rows")
    check("a different Colorado county gets no such caution",
          not d_other["Cautions"].str.contains("oil & gas", case=False, na=False).any())
    check("Fit_Score is identical whether or not the caution fired",
          d_weld["Fit_Score"].equals(d_other["Fit_Score"]),
          "scores must match row-for-row since only County Name differs")
    check("Fit_Tier is identical whether or not the caution fired",
          d_weld["Fit_Tier"].equals(d_other["Fit_Tier"]))

    # ── 16. A blank geography cell does not crash the screen ────────────────
    # Found 2026-08-11 by a fresh-install test, NOT by this suite or by any
    # real export: pandas is unpinned, resolved to 3.0, and 3.0's string dtype
    # stopped turning NaN into the literal text "nan" under .astype(str). Every
    # geography column is joined with " / " to build the peer-group key, so a
    # single empty County Name / Submarket Cluster threw
    # `TypeError: expected str instance, float found` and took the whole screen
    # down. Real CoStar exports routinely have some blank geography cells; the
    # one on this machine happens not to, which is exactly why neither this
    # suite nor months of live use ever hit it.
    print("\n16. Blank geography cells do not crash the screen")
    holey = src.copy()
    for col in ("County Name", "Submarket Cluster", "Submarket", "Market"):
        if col in holey.columns and len(holey) > 3:
            holey.loc[holey.index[:2], col] = float("nan")
    try:
        r_holey = _run(holey, full, tmp, "blank_geo")
        d_holey = r_holey["dataframe"]
        check("a screen survives blank County/Submarket/Market cells",
              len(d_holey) == len(holey),
              f"{len(d_holey)} rows out of {len(holey)} in")
        check("rows with a blank geography cell still get a Fit_Score",
              d_holey["Fit_Score"].notna().all())
        check("the blank never reaches output as the literal text 'nan'",
              not d_holey.astype(str).apply(
                  lambda c: c.str.contains(r"\bnan\b", case=False, na=False)).any().any())
    except Exception as e:
        check("a screen survives blank County/Submarket/Market cells", False,
              f"{type(e).__name__}: {e}")

    # ── 17. Finished-lot routing is informational only ──────────────────────
    # Added 2026-08-11. A listing whose own text says the land is already
    # platted gets compared against the firm's finished-lot acquisitions rather
    # than its entitlement plays -- the one case where a plan_type is passed
    # for an unowned listing, because "already platted" is a fact about the
    # asset, not a guess at what the firm would do. It must never touch the
    # ranking. Two copies of the same file, identical but for the platting
    # language, must score identically row for row.
    print("\n17. Finished-lot routing never moves the score")
    plat = src.copy()
    plat["Proposed Land Use"] = "Residential - 118 platted lots ready to build"
    noplat = src.copy()
    noplat["Proposed Land Use"] = "Residential"
    r_plat = _run(plat, full, tmp, "platted")
    r_noplat = _run(noplat, full, tmp, "notplatted")
    d_p, d_n = r_plat["dataframe"], r_noplat["dataframe"]
    check("Fit_Score is identical with and without platting language",
          d_p["Fit_Score"].reset_index(drop=True).equals(
              d_n["Fit_Score"].reset_index(drop=True)),
          "the routing is informational; it must not rank")
    check("Fit_Tier is identical with and without platting language",
          d_p["Fit_Tier"].reset_index(drop=True).equals(
              d_n["Fit_Tier"].reset_index(drop=True)))
    check("platting language does change which past deals are cited",
          d_p["Portfolio_Comparison"].str.contains("already-finished lots", na=False).sum()
          > d_n["Portfolio_Comparison"].str.contains("already-finished lots", na=False).sum(),
          "otherwise the routing is doing nothing at all")

    # ── 18. Jurisdiction dossiers inform, never rank ──────────────────────────
    # A city's researched dossier answers what the export cannot -- is this
    # jurisdiction going anywhere -- but it is prose a human wrote, and letting
    # prose move a score turns research into arithmetic. Same rule as Cautions
    # and Portfolio_Comparison, asserted the same way.
    print("\n18. Jurisdiction dossiers inform, never rank")
    from analysis.screening import jurisdiction_notes as jn

    check("a dossier is matched to its own city and state",
          bool(jn.note_for("Coolidge", "AZ", {("coolidge", "az"): "NOTE"})))
    check("a spelled-out state still matches its code",
          bool(jn.note_for("Coolidge", "Arizona", {("coolidge", "az"): "NOTE"})))
    # The dangerous one: same city name, different state. A postal code is NOT
    # a prefix of the state name ("arizona" starts with "ar" = ARKANSAS), and
    # an earlier version matched on prefix and would have handed Arizona's
    # water and impact-fee research to listings in other states.
    check("the same city name in ANOTHER state gets nothing",
          not jn.note_for("Coolidge", "TX", {("coolidge", "az"): "NOTE"}))
    check("Arkansas is never mistaken for Arizona",
          not jn.note_for("Coolidge", "AR", {("coolidge", "az"): "NOTE"}))
    check("a row with no state gets nothing rather than a guess",
          not jn.note_for("Coolidge", "", {("coolidge", "az"): "NOTE"}))
    check("a city with no dossier gets nothing",
          not jn.note_for("Buckeye", "AZ", {("coolidge", "az"): "NOTE"}))

    # And the whole point: the note must not be able to move the ranking.
    jdir = jn.jurisdictions_dir()
    if jdir and jdir.is_dir() and any(jdir.glob("*.md")):
        import shutil as _sh
        hidden = jdir.with_name(jdir.name + "_check_screener_tmp")
        _sh.move(str(jdir), str(hidden))
        try:
            bare = _run(src, full, tmp, "nodossier")["dataframe"]
        finally:
            _sh.move(str(hidden), str(jdir))
        withd = _run(src, full, tmp, "withdossier")["dataframe"]
        check("Fit_Score is identical with and without dossiers",
              withd["Fit_Score"].equals(bare["Fit_Score"]),
              "a dossier is context, never a score input")
        check("Fit_Tier is identical with and without dossiers",
              withd["Fit_Tier"].equals(bare["Fit_Tier"]))
        check("the column exists even when no dossier does",
              "Jurisdiction_Note" in bare.columns
              and (bare["Jurisdiction_Note"].astype(str).str.len() == 0).all(),
              "silence, not a placeholder implying the city was assessed")
    else:
        skip("dossiers never move the score", "no dossiers on this machine to test with")

    # ---- 19. The large-ask reference is measured against the listing's own market ----
    print("\n19. A big ask is measured against the market it is in")
    try:
        from analysis.screening.portfolio_comparison import load_index as _li
        _idx = _li()
    except Exception:
        _idx = None
    if not _idx:
        skip("the large-ask caution names the listing's own state",
             "no portfolio comparison index on this machine")
    else:
        import statistics as _st

        def _priced(st_code):
            return [r["entry_price_usd"] for r in _idx
                    if isinstance(r.get("entry_price_usd"), (int, float))
                    and r["entry_price_usd"] > 0
                    and (not st_code or (r.get("state") or "").upper() == st_code)]

        well_evidenced = [s for s in {(r.get("state") or "").upper() for r in _idx}
                          if s and len(_priced(s)) >= fs._MIN_STATE_SAMPLE]
        thin = [s for s in {(r.get("state") or "").upper() for r in _idx}
                if s and 0 < len(_priced(s)) < fs._MIN_STATE_SAMPLE]

        check("a state the firm has real history in is named by name",
              all(s in fs._purchase_reference(s) for s in well_evidenced),
              f"states with enough deals to quote: {sorted(well_evidenced) or 'none'}")

        # The trap this exists for: CO and NM have exactly ONE priced deal each.
        # A "median" from one purchase is a number pretending to be evidence.
        check("a state with too few deals is never given its own median",
              all("too few" in fs._purchase_reference(s) for s in thin),
              f"thin states: {sorted(thin) or 'none'}")
        check("  ...and says so out loud rather than staying silent",
              all(fs._purchase_reference(s).strip() for s in thin))
        check("a state the firm has never bought in says exactly that",
              "no recorded purchase history in ZZ" in fs._purchase_reference("ZZ"))

        firm_med = _st.median(_priced(None))
        check("the fallback quotes the whole-portfolio figure, not a state one",
              f"${firm_med/1e6:.1f}M" in fs._purchase_reference("ZZ"))

        # Two different markets must not receive the same sentence.
        if len(well_evidenced) >= 2:
            a, b = sorted(well_evidenced)[:2]
            check("two different markets get two different references",
                  fs._purchase_reference(a) != fs._purchase_reference(b),
                  f"{a} vs {b} -- a global figure would make these identical")

        # Same rule as every other caution: informational, never a score input.
        big = src.copy()
        pcol = next((c for c in ("For Sale Price", "Sale Price", "Price") if c in big.columns), None)
        if pcol:
            big[pcol] = 99_000_000
            az = _run(big, full, tmp, "bigask_az")["dataframe"]
            other = big.copy()
            for sc in ("State", "State Name"):
                if sc in other.columns:
                    other[sc] = "TX"
            tx = _run(other, full, tmp, "bigask_tx")["dataframe"]
            check("Fit_Score is identical whichever market the reference came from",
                  tx["Fit_Score"].equals(az["Fit_Score"]),
                  "the reference is context, never arithmetic")

    # ---- 20. The headline card compares entry to entry, never entry to by-exit ----
    # The report's biggest number is a total of ASKING PRICES. Setting it against
    # average invested capital (purchase plus years of entitlement and carry) made
    # every shortlist read several times cheaper against the firm than it was.
    print("\n20. The report's headline figures compare like with like")
    asm = base["assumptions"]
    check("the withdrawn 'average asset value' range is gone from the report data",
          not [k for k in asm if "avg_asset_value" in k],
          "it was never a range -- the two figures measure different things")

    buy = asm.get("typical_purchase_millions")
    inv = asm.get("avg_invested_capital_millions")
    if buy is None:
        skip("the acquisition figure is set against a purchase price",
             "no deal record on this machine to derive one from")
    else:
        check("the acquisition figure is set against a purchase price",
              buy > 0 and asm.get("typical_purchase_n", 0) > 0,
              f"derived from {asm.get('typical_purchase_n')} priced deals")
        check("  ...and it is NOT the by-exit invested-capital figure",
              inv is None or abs(buy - inv) > 1e-9,
              "the exact confusion this section exists to prevent")
        check("the purchase figure is derived, never stored in tracked code",
              "typical_purchase_millions" not in
              (PROJECT_ROOT / "analysis" / "screening" / "fit_screen.py").read_text(
                  encoding="utf-8").split("def _purchase_assumptions")[0].split(
                  '"typical_purchase_millions": None')[-1],
              "a real figure in a public repo is a leak, not a default")

    # A teammate machine has no cost file and no deal record, so BOTH figures
    # arrive as null. The report must then simply say less -- never print a zero,
    # which reads as a real measurement. Asserted against the template itself,
    # because that is where the guard has to live.
    tpl = (PROJECT_ROOT / "analysis" / "screening" / "report_template.html").read_text(
        encoding="utf-8")
    check("the report guards the purchase figure before printing it",
          "buyAvg!=null" in tpl.replace(" ", ""))
    check("the report guards the by-exit figure before printing it",
          "invCap!=null" in tpl.replace(" ", ""))
    check("the by-exit figure is its own sentence, not merged into the card label",
          'id="f-cap-n"' in tpl and "invested capital" in tpl,
          "merging them is what made every shortlist read cheap")

    # ---- 21. Cost figures reach the whole team, not just the maintainer ----
    # Measured 2026-08-13 before this was wired: a teammate with no cost record
    # scored 170 of 216 rows differently and shared only 3 of the top 10 with
    # this machine. Both runs were honest; nobody comparing two shortlists would
    # have guessed why they disagreed.
    print("\n21. Every machine screens from the same cost record")
    try:
        import config as _cfg
        shared_cost = Path(_cfg.ORG_SETTINGS_DIR) / "cost_assumptions.json"
    except Exception:
        shared_cost = None
    local_cost = PROJECT_ROOT / "data" / "cost_assumptions.json"

    if shared_cost is None:
        skip("the cost record is published where the team can read it",
             "no shared folder configured on this machine")
    else:
        check("the cost record is published where the team can read it",
              shared_cost.is_file(),
              f"{'found' if shared_cost.is_file() else 'MISSING'} in the team's settings folder")

    if not local_cost.is_file():
        # This IS the teammate case -- if it loaded, it came from the shared copy.
        check("a machine with no local copy still gets the cost record",
              bool(fs._COST),
              "read from the team's shared folder")
    elif shared_cost is not None and shared_cost.is_file():
        import json as _json
        try:
            same = _json.loads(local_cost.read_text(encoding="utf-8")) == \
                   _json.loads(shared_cost.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            same = False
        check("the published copy matches this machine's own",
              same,
              "a stale published copy makes the team disagree without saying so")
        check("local is preferred, so a deliberate local file still wins",
              "data" in fs._load_cost_assumptions.__doc__ and
              "LOCAL FIRST" in fs._load_cost_assumptions.__doc__)

    # ---- 22. A bad file in the TEAM folder must never take the system down ----
    # Every file below is resolved local-then-shared, and the shared copy sits in
    # a OneDrive folder every teammate can write to. A truncated sync or a
    # hand-edit produces WELL-FORMED JSON OF THE WRONG SHAPE -- a third failure
    # mode, distinct from "missing" and "corrupt", and the one that used to slip
    # through to somebody's .get(). Measured 2026-08-13: a list in the cost file
    # crashed fit_screen at import (so the whole MCP server died before serving a
    # single tool), and a wrong-shaped comparison index broke both
    # compare_to_portfolio_history and every screen. Claude Desktop reports a
    # crash and a hang identically as "the server isn't responding".
    print("\n22. A wrong-shaped file in the team folder degrades, never crashes")
    import json as _js
    import shutil as _sh
    import analysis.screening.portfolio_comparison as _pc

    BAD_SHAPES = [("a list", []), ("a string", "x"), ("a number", 42), ("null", None)]

    def _survives(target, call):
        """True if `call` survives every wrong shape written to `target`."""
        if target is None:
            return None
        bak = target.with_suffix(target.suffix + ".checkbak") if target.exists() else None
        if bak:
            _sh.copy2(target, bak)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            for _, shape in BAD_SHAPES:
                target.write_text(_js.dumps(shape), encoding="utf-8")
                try:
                    call()
                except Exception:
                    return False
            return True
        finally:
            if bak:
                _sh.move(str(bak), str(target))
            else:
                target.unlink(missing_ok=True)

    facts = {"state": "AZ", "land_type": "residential", "acres": 100}
    idx = _pc.index_path()
    ok = _survives(idx, lambda: _pc.find_similar_deals(facts))
    check("a wrong-shaped comparison index still lets a comparison run",
          ok, "it used to raise AttributeError on .get()")
    ok = _survives(idx, lambda: fs._purchase_reference("AZ"))
    check("  ...and still lets the large-ask note render", ok)

    try:
        import config as _c
        cost_target = Path(_c.ORG_SETTINGS_DIR) / "cost_assumptions.json"
    except Exception:
        cost_target = None
    ok = _survives(cost_target, lambda: fs._load_cost_assumptions().get("x"))
    if ok is None:
        skip("a wrong-shaped cost file leaves a usable (empty) record",
             "no shared folder on this machine")
    else:
        check("a wrong-shaped cost file leaves a usable (empty) record", ok,
              "a list here used to kill the server at import")

    # ---- 23. The stuck-deal caution informs, never ranks ----
    # Asked for on 2026-08-14 as a SCORING factor and built as a caution
    # instead: only 6 of 49 properties are evidenced as marketed-and-unsold,
    # which is thinner than the 4 completed exits this project already refused
    # to score on. Same rule as every other caution.
    print("\n23. The stuck-deal caution informs, never ranks")
    import analysis.screening.portfolio_comparison as _pc

    if not _pc.load_index():
        skip("the stuck-deal caution never moves the score", "no deal record on this machine")
    else:
        with_c = base["dataframe"]
        real_fn = _pc.stuck_deal_caution
        _pc.stuck_deal_caution = lambda *a, **k: None
        try:
            without = _run(src, full, tmp, "nostuck")["dataframe"]
        finally:
            _pc.stuck_deal_caution = real_fn
        check("Fit_Score is identical with and without the stuck-deal caution",
              with_c["Fit_Score"].equals(without["Fit_Score"]),
              "it is context, never arithmetic")
        check("Fit_Tier is identical with and without it",
              with_c["Fit_Tier"].equals(without["Fit_Tier"]))

        # Firing everywhere is the same as not firing at all. Matching on state
        # and land type alone hit 71 of 216 rows before county was required.
        hit = with_c["Cautions"].astype(str).str.contains("marketed and could not sell")
        share = hit.mean()
        check("it stays rare enough to mean something",
              share <= 0.25,
              f"fires on {int(hit.sum())} of {len(with_c)} rows ({share:.0%})")

        # It must never fire without evidence behind it.
        check("it never fires on a county the firm has no stuck position in",
              _pc.stuck_deal_caution({"state": "ZZ", "county": "nowhere",
                                      "land_type": "residential"}) is None)
        check("  ...nor when the land type is unknown",
              _pc.stuck_deal_caution({"state": "AZ", "county": "pinal",
                                      "land_type": ""}) is None)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
