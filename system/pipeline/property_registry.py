"""
A stable internal ID for every portfolio property.

WHY THIS EXISTS. The same property is named four slightly different ways
across the four files that describe it -- the Smartsheet Project Master, the
coordinates table, the comparison index, and its own summary filename. A
Project Master name often carries a parenthetical alias or a phase suffix that
one of the others dropped. Measured 2026-08-11: substring matching links 49/49
to coordinates and summaries but only 48/49 to the comparison index, and the
one failure is a genuine parenthetical mismatch.

Nothing joins those files by name today, which is the only reason that has been
harmless. The moment anything does, a quarter of the portfolio drops out
silently -- no error, no warning, just missing rows. This module is the
insurance: one durable ID per property, with every observed spelling recorded
as an alias, so a future join can be exact instead of hopeful.

DELIBERATELY INVISIBLE. No ID is ever shown to a user, printed in a report, or
mentioned by a tool. People refer to properties by name, and always will; this
is plumbing that exists so the machine can stop guessing.

IDS ARE NEVER REUSED AND NEVER CHANGE. They are assigned once, in the order
properties are first seen, and persisted. A property that gets renamed keeps
its ID and gains an alias -- which is the entire point. Regenerating the
registry is therefore additive and safe: it can learn new aliases and new
properties, but it will not renumber anything that already exists.

The registry file holds real property names, so it lives under system/data/
and is gitignored like every other real-data file.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger("vaulter.registry")

REGISTRY_FILENAME = "property_ids.json"


def registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / REGISTRY_FILENAME


def _norm(s) -> str:
    """Strip everything that varies between spellings of the same name."""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def load_registry(data_dir: Path) -> dict:
    """
    {property_id: {"canonical_name": str, "aliases": [str, ...]}}

    A missing file is normal (nothing built yet) and returns {} rather than
    raising -- every caller treats an unresolved name the same way it did
    before this module existed.
    """
    path = registry_path(data_dir)
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning(f"[REGISTRY] Could not read {path}: {e}")
        return {}
    # Valid JSON of the wrong shape is a third failure mode, distinct from
    # unreadable and unparseable, and it used to slip through to a caller's
    # `.items()`. This file resolves local-then-shared, so the copy in the
    # team folder is writable by everyone; a truncated sync yields well-formed
    # JSON that is not a dictionary. Same outcome as a missing file.
    if not isinstance(loaded, dict):
        log.warning(f"[REGISTRY] Ignoring {path}: expected a JSON object, "
                    f"got {type(loaded).__name__}.")
        return {}
    return loaded


def resolve(data_dir: Path, name: str, registry: dict | None = None) -> str | None:
    """
    Name -> property_id, or None if it genuinely cannot be identified.

    Exact normalized match first, then a single unambiguous substring match --
    the same escalation `property_coordinates.lookup()` uses, and it refuses on
    ambiguity for the same reason: silently picking one of several candidates
    is how a wrong answer gets produced with nothing to indicate it.
    """
    reg = registry if registry is not None else load_registry(data_dir)
    if not reg:
        return None

    target = _norm(name)
    if not target:
        return None

    for pid, rec in reg.items():
        for known in [rec.get("canonical_name", "")] + list(rec.get("aliases", [])):
            if _norm(known) == target:
                return pid

    hits = [
        pid for pid, rec in reg.items()
        if any(_norm(k) and (_norm(k) in target or target in _norm(k))
               for k in [rec.get("canonical_name", "")] + list(rec.get("aliases", [])))
    ]
    return hits[0] if len(hits) == 1 else None


def canonical_name(data_dir: Path, property_id: str,
                   registry: dict | None = None) -> str | None:
    reg = registry if registry is not None else load_registry(data_dir)
    rec = reg.get(property_id)
    return rec.get("canonical_name") if rec else None


def build_registry(data_dir: Path, names_by_source: dict[str, list[str]]) -> dict:
    """
    Create or extend the registry from {source_label: [names]}.

    Additive by design. An existing property keeps its ID and simply gains any
    new spelling as an alias; only a genuinely unrecognised name gets a new ID.
    That is what makes this safe to re-run after every Smartsheet export.

    The FIRST source given is treated as canonical (in practice the Project
    Master, which is the read-only source of truth for which properties exist).
    """
    reg = load_registry(data_dir)
    next_n = 1 + max((int(pid.split("-")[-1]) for pid in reg if "-" in pid), default=0)

    sources = list(names_by_source.items())
    added, aliased = [], []

    def _exact(target: str) -> str | None:
        t = _norm(target)
        for pid, rec in reg.items():
            if _norm(rec["canonical_name"]) == t:
                return pid
        return None

    for i, (label, names) in enumerate(sources):
        canonical_source = (i == 0)
        for name in names:
            if not str(name or "").strip():
                continue

            if canonical_source:
                # EXACT match only. The canonical source defines which
                # properties exist, so every distinct name in it is a distinct
                # property -- full stop. Substring matching here silently
                # merged two real, separate properties whose names share a
                # stem (a project and its later phase), which is precisely the
                # class of error this registry exists to eliminate. Measured
                # 2026-08-11: it produced 48 IDs for 49 properties.
                pid = _exact(name)
            else:
                pid = resolve(data_dir, name, registry=reg)

            if pid is None:
                if not canonical_source:
                    # A name in a secondary file that matches nothing is worth
                    # knowing about, but it must never invent a property --
                    # only the canonical source can do that.
                    log.debug(f"[REGISTRY] '{name}' from {label} matched nothing")
                    continue
                pid = f"prop-{next_n:04d}"
                next_n += 1
                reg[pid] = {"canonical_name": str(name), "aliases": []}
                added.append((pid, name))
            else:
                rec = reg[pid]
                known = {_norm(rec["canonical_name"])} | {_norm(a) for a in rec["aliases"]}
                if _norm(name) not in known:
                    rec["aliases"].append(str(name))
                    aliased.append((pid, name))

    path = registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)

    return {"registry": reg, "added": added, "aliased": aliased}
