"""
add_summary_cards.py
--------------------
Give every property summary its data card, and move its Sources to the end.

    python scripts/add_summary_cards.py            # dry run: says what it WOULD do
    python scripts/add_summary_cards.py --write    # backs every file up, then writes
    python scripts/add_summary_cards.py --folder <path>   # a copy, for testing

One-off for the 2026-09-24 restructure, kept because it is also the safe way
to add a card to a summary someone writes by hand later: a file that already
has a card is left alone.

What it does to each file, and nothing else:
  1. Builds the card from the comparison index record of the same filename
     (`portfolio_comparison_index.json` -- 48 of 49 backed by documents), the
     dates already written in the file, and the property registry's aliases.
     Nothing is invented: a fact the index does not have is null.
  2. Inserts the card after the title line.
  3. Moves the `**Sources:**` block to a `## Sources` section at the very
     end. The `**Source files as of:**` line STAYS where it is, directly under
     the card: installs on the pre-card code find the date by its first
     mention in the file, and one summary mentions an older date inside an
     update section -- moving the line below that would have told every
     old install the summary was behind. Everything else stays where it is.

A file whose sources cannot be found is reported and skipped, never guessed
at. `--write` copies every file to `system/data/backups/property_summaries_<date>/`
first, because these files are shared with the whole team through OneDrive.
"""

import argparse
import datetime as _dt
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from summaries import (CARD_FIELDS, FORMAT_VERSION, OPTIONAL_FIELDS,  # noqa: E402
                       card_problems, iter_summaries, parse_card, render_card)

_STAMP_LINE = re.compile(r"^\*\*Source files as of:?\*\*.*$", re.M)
_SOURCES_LINE = re.compile(r"^\*\*Sources[^\n]*?:?\*\*", re.M)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UPDATE_HEADING = re.compile(r"^#{2,3} .*?(\d{4}-\d{2}-\d{2})", re.M)


def _dates_from_stamp(stamp_line: str):
    """(source_files_as_of, summary_written) as the stamp line states them."""
    dates = _DATE.findall(stamp_line)
    as_of = dates[0] if dates else None
    m = re.search(r"writ(?:ten|e)\D{0,40}?(\d{4}-\d{2}-\d{2})", stamp_line, re.I)
    written = m.group(1) if m else (dates[1] if len(dates) > 1 else None)
    return as_of, written


def _lift_block(text: str, pattern: re.Pattern, whole_block: bool = True):
    """Remove the line matching `pattern` -- plus, when `whole_block`, the
    non-blank lines that follow it -- returning (text_without_it, lifted).
    A block ends at the first blank line, the shape every summary uses."""
    m = pattern.search(text.split("\n## ", 1)[0])
    if not m:
        return text, ""
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n\n" if whole_block else "\n", m.start())
    if end == -1:
        end = len(text)
    block = text[start:end].rstrip("\n")
    rest = text[:start] + text[end:].lstrip("\n")
    return rest, block


def _lift_sources_section(text: str):
    """One summary keeps its sources under a `## Sources` heading ABOVE its
    findings rather than in a `**Sources:**` block. Lift that whole section
    (heading to the next `## `) so it can be re-placed at the end."""
    m = re.search(r"^## Sources[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    if not m or m.start() > text.find("\n## Findings"):
        return text, ""
    return text[:m.start()] + text[m.end():], m.group(1).strip("\n")


def build_card(name: str, text: str, index_rec: dict, aliases: list) -> dict:
    stamp = _STAMP_LINE.search(text)
    as_of, written = _dates_from_stamp(stamp.group(0)) if stamp else (None, None)
    if not written:
        m = re.search(r"^\*\*Written:?\*\*\s*(\d{4}-\d{2}-\d{2})", text, re.M)
        written = m.group(1) if m else None
    heading_dates = _UPDATE_HEADING.findall(text)
    last = max(heading_dates) if heading_dates else written

    card = {k: index_rec.get(k) for k in CARD_FIELDS if k in index_rec}
    card["format_version"] = FORMAT_VERSION
    for k in OPTIONAL_FIELDS:
        if index_rec.get(k) not in (None, ""):
            card[k] = index_rec[k]
    card["property"] = index_rec.get("property_name") or text.splitlines()[0].lstrip("# ").strip()
    card["aliases"] = [a for a in aliases if a and a != card["property"]]
    card["summary_written"] = written
    card["source_files_as_of"] = as_of
    card["last_updated"] = last
    # The index stores a few numbers as strings ("unclear"); a card keeps
    # numbers as numbers and unknowns as null.
    for k in ("acres", "entry_year", "entry_price_usd", "exit_year",
              "exit_price_usd", "hold_years", "gross_price_multiple"):
        v = card.get(k)
        if isinstance(v, str):
            try:
                card[k] = float(v) if "." in v else int(v)
            except ValueError:
                card[k] = None
    return card


def convert(text: str, card: dict) -> tuple[str, str]:
    """(new_text, problem). A non-empty problem means the file was NOT converted."""
    if parse_card(text) is not None:
        return text, "already has a card"
    problems = card_problems(card)
    if problems:
        return text, "card would be unusable: " + "; ".join(problems)
    body, sources = _lift_block(text, _SOURCES_LINE)
    if not sources:
        body, sources = _lift_sources_section(body)
    if not sources:
        return text, "no **Sources:** block found near the top"
    lines = body.split("\n", 1)
    title, rest = lines[0], (lines[1] if len(lines) > 1 else "")
    rest = rest.lstrip("\n")
    new = title + "\n\n" + render_card(card) + "\n\n" + rest.rstrip("\n") + "\n"
    new += "\n## Sources\n\n" + sources + "\n"
    return new, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--folder", type=Path, default=None)
    args = ap.parse_args()

    import config
    from analysis.screening import portfolio_comparison as pc
    from pipeline import property_registry as pr

    folder = args.folder or Path(config.PROPERTY_SUMMARIES_DIR)
    index = {r.get("filename"): r for r in pc.load_index(pc.INDEX_PATH)}
    if not index:
        print("The comparison index is not on this machine; the cards would be empty. Stopping.")
        return 1
    registry = pr.load_registry(Path(config.DATA_DIR))

    converted, skipped, cards_written_null = [], [], 0
    backup = None
    if args.write:
        backup = Path(config.DATA_DIR) / "backups" / f"property_summaries_{_dt.date.today():%Y%m%d}"
        backup.mkdir(parents=True, exist_ok=True)

    for p, text in iter_summaries(folder):
        rec = index.get(p.name)
        if rec is None:
            skipped.append((p.name, "no comparison-index record with this filename"))
            continue
        aliases = []
        pid = pr.resolve(Path(config.DATA_DIR), rec.get("property_name", ""), registry)
        if pid:
            r = registry.get(pid, {})
            aliases = [r.get("canonical_name")] + list(r.get("aliases", []))
        card = build_card(p.name, text, rec, aliases)
        new, problem = convert(text, card)
        if problem:
            skipped.append((p.name, problem))
            continue
        if card.get("summary_written") is None:
            cards_written_null += 1
        converted.append(p.name)
        if args.write:
            shutil.copy2(p, backup / p.name)
            p.write_text(new, encoding="utf-8")

    verb = "Converted" if args.write else "Would convert"
    print(f"{verb} {len(converted)} summar{'y' if len(converted) == 1 else 'ies'}"
          f"; {cards_written_null} of them have no 'summary written' date the file states.")
    for name, why in skipped:
        print(f"  skipped  {name}: {why}")
    if backup:
        print(f"Originals backed up to {backup}")
    return 0 if not skipped else 2


if __name__ == "__main__":
    sys.exit(main())
