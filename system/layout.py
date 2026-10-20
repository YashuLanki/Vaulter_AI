"""
layout.py
---------
Where the team folder keeps screens. Copied from the plugin's engine/layout.py (2026-10-20)
so the old server and the plugin file every screen in the same place under the same name;
change both together.

Two layouts, chosen by whether Screen/ exists in the team folder (config.NEW_LAYOUT):

  * OLD: CoStar Drop/, output/screening/, output/proximity/.
  * NEW (owner, 2026-10-20):
        Screen/Drop/            every listing file people bring in, screened or not
        Screen/<Metro ST>/      ONLY what a screen made: "<date> <Metro ST> - Ranking.xlsx"
                                and "... - Report.html"
        Proximity/              "what's nearby" files
"""
from __future__ import annotations

import collections
import re
from pathlib import Path


def new_layout() -> bool:
    from config import NEW_LAYOUT
    return bool(NEW_LAYOUT)


def _clean(name: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name)
    return re.sub(r"\s+", " ", name).strip(" .") or "Unknown market"


def market_name(markets, states=(), counties=()) -> str:
    """
    The market a listing file is about, as a folder name: "Phoenix AZ". The market most of
    its listings are in (CoStar writes "Phoenix, AZ"); a market with no state takes the
    file's commonest state ("Texas Central TX"); with no market column, the commonest
    county ("Maricopa County AZ").
    """
    blank = {"", "nan", "none", "null"}
    pick = lambda xs: (collections.Counter(str(x).strip() for x in xs
                                           if x is not None and str(x).strip().lower() not in blank)
                       .most_common(1) or [("", 0)])[0][0]
    st = pick(states)
    m = pick(markets)
    if m:
        m = re.sub(r"\s+USA$", "", m)
        if re.search(r",\s*[A-Z]{2}$", m) or re.search(r"\s[A-Z]{2}$", m):
            return _clean(m.replace(",", " "))
        return _clean(f"{m} {st}" if st else m)
    c = pick(counties)
    if c:
        c = c if c.lower().endswith(" county") else f"{c} County"
        return _clean(f"{c} {st}" if st else c)
    return "Unknown market"


def screen_dir(market: str) -> Path:
    """Where a screen's ranking and report go: the market's folder, or the old output folder."""
    from config import SCREENING_OUTPUT_DIR, SCREEN_DIR
    return (Path(SCREEN_DIR) / _clean(market)) if new_layout() else Path(SCREENING_OUTPUT_DIR)


def result_label(stem: str, market: str, registry: Path | None = None, drop: Path | None = None) -> str:
    """
    The readable name a screen's results go by (owner, 2026-10-20): "2026-10-02 Atlanta GA",
    the date its listing file was saved and its market, made unique with " (2)" when two
    screens share both. Kept in the screens record under the screen's stem, so screening the
    same data again writes over its own files. Never raises; falls back to the stem.
    """
    import datetime as _d
    import json
    try:
        reg_path = Path(registry) if registry else screens_registry()
        try:
            reg = json.loads(reg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reg = {}
        reg = reg if isinstance(reg, dict) else {}
        entry = reg.get(stem) if isinstance(reg.get(stem), dict) else None
        if entry and isinstance(entry.get("label"), str) and entry["label"].strip():
            return entry["label"]
        if drop is None:
            from config import COSTAR_DROP_DIR as drop
        names = [s for s in (entry or {}).get("sources", []) if isinstance(s, str)]
        names += [stem + ext for ext in (".xlsx", ".xls", ".xlsm", ".csv")]
        when = next((_d.date.fromtimestamp((Path(drop) / n).stat().st_mtime)
                     for n in names if (Path(drop) / n).is_file()), None)
        if when is None and entry and str(entry.get("last_screened", ""))[:10]:
            when = _d.date.fromisoformat(str(entry["last_screened"])[:10])
        base = _clean(f"{(when or _d.date.today()).isoformat()} {market}")
        taken = {e.get("label") for k, e in reg.items() if k != stem and isinstance(e, dict)}
        label, n = base, 2
        while label in taken:
            label, n = f"{base} ({n})", n + 1
        if entry is not None:
            entry["label"] = label
            reg_path.write_text(json.dumps(reg, indent=1), encoding="utf-8")
        return label
    except Exception:  # noqa: BLE001 -- naming must never stop a screen
        return stem


def ranking_name(stem: str, market: str = "") -> str:
    return f"{result_label(stem, market)} - Ranking.xlsx" if new_layout() else f"fit_screen_{stem}.xlsx"


def report_name(stem: str, market: str = "") -> str:
    return f"{result_label(stem, market)} - Report.html" if new_layout() else f"screen_{stem}.html"


def screens_registry() -> Path:
    """The small record of which data each result was built from (kept out of the market folders)."""
    from config import SCREENING_OUTPUT_DIR, SHARED_SYSTEM_DIR
    return (Path(SHARED_SYSTEM_DIR) / "screens.json") if new_layout() \
        else Path(SCREENING_OUTPUT_DIR) / ".screens.json"


def all_screen_reports() -> list[Path]:
    """Every screening report page, newest first."""
    from config import SCREENING_OUTPUT_DIR, SCREEN_DIR
    pages = (list(Path(SCREEN_DIR).glob("*/* - Report.html")) if new_layout()
             else list(Path(SCREENING_OUTPUT_DIR).glob("screen_*.html")))
    return sorted(pages, key=lambda p: p.stat().st_mtime, reverse=True)
