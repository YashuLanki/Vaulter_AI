"""
summaries.py
------------
The data card at the top of every property summary, and the one place that
reads or writes it.

A summary in `config.PROPERTY_SUMMARIES_DIR` opens with its title line and
then a fenced ```json block -- the "card". The card states, in a fixed layout,
the handful of facts a program needs about the deal (where, what kind of land,
how big, when bought, what the plan was, how it turned out) and about the
summary itself (when it was written, when its sources were last checked, when
it was last updated). Everything below the card is prose for people.

Why a card rather than a separate list (2026-09-24): the comparison facts used
to live in `portfolio_comparison_index.json`, a separate file an agent had to
rebuild by hand whenever a summary changed -- and it went five days stale once
without anyone noticing. With the facts inside the summary they describe, the
comparison can never drift from the summary. `portfolio_comparison.load_index`
now reads the cards; the JSON file survives only as a fallback for a machine
that cannot reach the team folder.

Three rules, each load-bearing:

* **The title stays on line 1.** `mcp_server._search_needles` reads it to find
  the property's folder on the drive, and old installs read it that way too.
* **A card that fails to parse is reported, never guessed at.** `parse_card`
  returns None and says why; callers treat that summary as having no card,
  which every use site already handles (it is what a summary written before
  the card existed looks like).
* **Vocabulary is checked here, once.** `land_type`, `plan_type` and
  `outcome_status` must be one of the fixed values `portfolio_comparison`
  scores on; a typo would otherwise silently drop that deal out of every
  comparison, which is the invisible failure this project distrusts most.
"""

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("vaulter.summaries")

FORMAT_VERSION = 1

# Every card carries these, in this order, null where genuinely unknown.
CARD_FIELDS = (
    "format_version",
    "property", "aliases",
    "state", "county",
    "land_type", "acres",
    "entry_year", "entry_price_usd",
    "plan_type", "plan_type_source",
    "outcome_status", "disposition_detail",
    "exit_year", "exit_price_usd", "exit_form", "hold_years", "gross_price_multiple",
    "notes",
    "summary_written", "source_files_as_of", "last_updated",
)
# Carried only when the record has them -- provenance prose that would be
# noise as a permanent null on the other 45 files.
OPTIONAL_FIELDS = ("exit_note", "disposition_source", "disposition_source_note")

# The fields a card cannot do without. `state` and `land_type` are what the
# comparison anchors on; the dates are what the health check needs.
REQUIRED_FIELDS = ("format_version", "property", "state", "land_type",
                   "plan_type", "outcome_status", "source_files_as_of")

_FENCE = re.compile(r"^```json[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _vocab():
    from analysis.screening.portfolio_comparison import (
        LAND_TYPES, PLAN_TYPES, OUTCOME_STATUSES)
    return LAND_TYPES, PLAN_TYPES, OUTCOME_STATUSES


def card_problems(card) -> list[str]:
    """Why this card cannot be trusted, in plain words. Empty means it can."""
    if not isinstance(card, dict):
        return [f"the card is a {type(card).__name__}, not an object"]
    problems = [f"missing '{k}'" for k in REQUIRED_FIELDS if card.get(k) in (None, "")]
    land, plan, outcome = _vocab()
    for key, allowed in (("land_type", land), ("plan_type", plan),
                         ("outcome_status", outcome)):
        v = card.get(key)
        if v not in (None, "") and v not in allowed:
            problems.append(f"'{key}' is '{v}', not one of {sorted(allowed)}")
    for key in ("summary_written", "source_files_as_of", "last_updated"):
        v = card.get(key)
        if v not in (None, "") and not (isinstance(v, str) and _DATE.match(v)):
            problems.append(f"'{key}' is '{v}', not a YYYY-MM-DD date")
    if card.get("aliases") is not None and not isinstance(card.get("aliases"), list):
        problems.append("'aliases' is not a list")
    return problems


def parse_card(text: str, name: str = "summary") -> dict | None:
    """
    The card at the top of a summary, or None with the reason logged.

    Only the FIRST fenced json block is considered, and only if it appears
    before the first `## ` heading -- a json block quoted somewhere in the
    body is content, not the card.
    """
    head = text.split("\n## ", 1)[0]
    m = _FENCE.search(head)
    if not m:
        return None
    try:
        card = json.loads(m.group(1))
    except ValueError as e:
        log.warning(f"[SUMMARIES] {name}: the data card is not valid JSON ({e}); "
                    f"treating this summary as having no card.")
        return None
    problems = card_problems(card)
    if problems:
        log.warning(f"[SUMMARIES] {name}: the data card cannot be used -- "
                    f"{'; '.join(problems)}.")
        return None
    return card


def render_card(card: dict) -> str:
    """The card as it is written into a file: fixed field order, 2-space indent."""
    ordered = {k: card.get(k) for k in CARD_FIELDS}
    ordered["format_version"] = FORMAT_VERSION
    if ordered.get("aliases") is None:
        ordered["aliases"] = []
    for k in OPTIONAL_FIELDS:
        if card.get(k) not in (None, ""):
            ordered[k] = card[k]
    return "```json\n" + json.dumps(ordered, indent=2, ensure_ascii=False) + "\n```"


def replace_card(text: str, card: dict) -> str:
    """The summary text with its card swapped for `card` (or inserted after
    the title if it had none). The rest of the file is untouched."""
    head, sep, tail = text.partition("\n## ")
    m = _FENCE.search(head)
    if m:
        head = head[:m.start()] + render_card(card) + head[m.end():]
    else:
        lines = head.split("\n", 1)
        head = lines[0] + "\n\n" + render_card(card) + ("\n" + lines[1] if len(lines) > 1 else "\n")
    return head + sep + tail


def iter_summaries(folder: Path):
    """(path, text) for every property summary in the folder -- the record
    files (`_passed-on-deals.md`, `_sold-deals.md`) are not summaries."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    for p in sorted(folder.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            yield p, p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            log.warning(f"[SUMMARIES] Could not read {p.name}: {e}")


def load_cards(folder: Path) -> list[dict]:
    """
    Every usable card in the folder, each with `filename` and `property_name`
    filled in so it is a drop-in for a `portfolio_comparison` index record.
    A summary with no card, or an unusable one, is simply absent -- and
    logged, never silently counted as fine.
    """
    out = []
    for p, text in iter_summaries(folder):
        card = parse_card(text, p.name)
        if card is None:
            continue
        rec = dict(card)
        rec["filename"] = p.name
        rec["property_name"] = card.get("property")
        out.append(rec)
    return out
