"""
Find the passages in the team's property summaries that answer a question.

Built 2026-09-29 because reading a summary to answer one fact cost 7,000 to
20,000 tokens: the summary tool could only hand over the whole file. This
returns the data card and the few passages that match, with the section each
came from, for a few hundred to a couple of thousand tokens.

It runs over the summaries ONLY -- never the document library -- for the
reasons the July rebuild removed the old vector database: indexing documents
means downloading all of them, and a shared index of their text would cross
SharePoint's folder permissions. The summaries already sit in the team folder
that everyone can read, so searching them crosses nothing.

Ranking is keyword ranking (BM25, the standard search-engine formula), with a
small hand-written list of the firm's synonyms ("sale price" / "consideration",
"closing" / "COE"). No model, no key, nothing to install, and it can be tested
for right and wrong answers. A meaning-based model is the next step only if the
retrieval check shows keyword ranking missing things.

What happens when it picks wrong is the design, not an afterthought. The
first version labelled its own result strong, partial or weak. Measured on 40
questions rephrased the way a teammate asks them (scripts/check_retrieval.py),
NO score separated right answers from wrong ones -- an unanswerable question
outscored most correct answers -- so the label told Claude to trust misses:
16 of 50 answerable-or-not questions came back wrong and "strong". A keyword
search cannot know whether it found the answer; the reader of the passages
can. So:

  * It returns CANDIDATES, never a verdict, and every reply tells Claude to
    read the whole summary if no passage plainly states the answer.
  * Ten passages by default: the answer was among them 31 times in 40, at
    about 1,000 tokens against 5,500 for the whole summary. A miss costs the
    candidates plus the full read, so a wrong pick costs a little, never a
    wrong answer.
  * The data card's figures (purchase price, year, status, sale) lead every
    reply, because several misses were prices the card already held.
  * Every reply lists the sections it did not show, so a miss is visible.
  * Across properties, it says a property missing from the list is not
    evidence the property lacks the thing asked about.

The "## Update history" section (updates kept verbatim after being folded into
the main text) and the Sources list are never searched: the first would surface
superseded facts, the second is a list of file names that would outscore prose.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

# ─── Words ────────────────────────────────────────────────────────────────────

_STOP = set("""
a an and are as at be been but by can could did do does for from had has have how i if in into is
it its of on or our that the their them then there these they this to was we were what when where
which who whom why will with would you your any all about after before did does ever get got
has have much many any tell me please know there's what's whats
""".split())

# Words people type -> words the summaries use. Each group is interchangeable.
# Hand-written from the firm's own vocabulary; add to it when the retrieval
# check shows a miss, never by guessing.
_SYNONYM_GROUPS = [
    {"price", "consideration", "purchase", "paid", "cost", "sold", "sale", "sell", "bought", "basis"},
    {"close", "closing", "closed", "coe", "escrow", "settlement"},
    {"loi", "letter", "intent", "offer", "offers"},
    {"psa", "contract", "agreement", "purchase"},
    {"buyer", "purchaser", "acquirer", "builder"},
    {"seller", "vendor", "owner"},
    {"flood", "floodplain", "sfha", "fema", "floodway"},
    {"water", "assured", "adwr", "well", "wells"},
    {"sewer", "wastewater", "septic", "lift"},
    {"hoa", "association", "ccr", "ccrs", "poa"},
    # "zone" is deliberately in no group: it means a flood zone as often as zoning.
    {"zoning", "rezone", "rezoning", "pud", "entitlement", "entitlements", "entitled"},
    {"plat", "platted", "map", "ttm", "subdivision", "lots"},
    {"acre", "acres", "acreage", "size", "big", "large"},
    {"extension", "extended", "extend"},
    {"survey", "alta", "topo"},
    {"environmental", "esa", "phase", "contamination"},
    {"loan", "lender", "debt", "refinance", "refi", "note"},
    {"city", "town", "municipality", "county", "jurisdiction"},
    {"annex", "annexation", "annexed"},
    {"road", "roads", "access", "freeway", "highway"},
    {"signed", "executed", "approved", "recorded"},
    {"dispute", "lawsuit", "litigation", "claim"},
    {"tax", "taxes", "assessment", "cfd", "pid", "tidd", "district"},
    # Added 2026-09-29 for searching inside documents (corpus/find.py).
    {"deposit", "earnest", "emd"},
    {"inspection", "feasibility", "diligence", "contingency"},
    {"deadline", "expire", "expiration", "terminate", "termination"},
    {"broker", "commission"},
    {"title", "policy", "commitment", "insurance"},
]
_SYN = {}
for _g in _SYNONYM_GROUPS:
    for _w in _g:
        _SYN.setdefault(_w, set()).update(_g - {_w})


def _stem(w: str) -> str:
    w = w.lower().strip("'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 4 and w.endswith("ed") and not w.endswith("eed"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())]


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in _words(text) if w not in _STOP and len(w) > 1]


# ─── Passages ─────────────────────────────────────────────────────────────────

_SKIP_SECTIONS = ("## update history", "## sources")
_MAX_PASSAGE = 1100


def passages(text: str, filename: str, prop: str) -> list[dict]:
    """
    Split one summary into passages, each carrying the headings above it.
    A bullet with its continuation lines is one passage; paragraphs under one
    heading are merged up to _MAX_PASSAGE characters. The data card, the
    update history and the Sources list are left out.
    """
    text = re.sub(r"```json.*?```", "", text, count=1, flags=re.S)
    out, heads, buf = [], {}, []
    skipping = False

    def flush():
        body = "\n".join(buf).strip()
        buf.clear()
        if len(body) < 25:
            return
        path = " › ".join(heads[k] for k in sorted(heads))
        out.append({"file": filename, "property": prop, "heading": path, "text": body})

    for line in text.split("\n"):
        m = re.match(r"^(#{2,4})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            if level == 2:
                skipping = line.strip().lower().startswith(_SKIP_SECTIONS)
            for k in [k for k in heads if k >= level]:
                del heads[k]
            heads[level] = m.group(2).strip()
            continue
        if skipping or line.startswith("# "):
            continue
        starts_item = bool(re.match(r"^\s{0,1}([-*]|\d+\.)\s", line))
        if (starts_item and buf) or (not line.strip() and buf) or \
                sum(len(x) for x in buf) > _MAX_PASSAGE:
            flush()
        if line.strip():
            buf.append(line)
    flush()
    return out


# ─── The index, cached by each file's modified time ──────────────────────────

_CACHE: dict = {}


def load_all(summaries_dir: Path) -> list[dict]:
    """Every passage of every summary, re-reading only files that changed."""
    from summaries import parse_card
    seen = set()
    folder = Path(summaries_dir)
    files = sorted(f for f in folder.glob("*.md") if not f.name.startswith("_")) if folder.is_dir() else []
    for path in files:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        seen.add(path.name)
        hit = _CACHE.get(path.name)
        if hit and hit[0] == mtime:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        card = parse_card(text) or {}
        prop = card.get("property") or text.split("\n", 1)[0].lstrip("# ").strip()
        ps = passages(text, path.name, prop)
        for p in ps:
            p["terms"] = _terms(p["heading"] + " " + p["text"])
            p["head_terms"] = set(_terms(p["heading"]))
        _CACHE[path.name] = (mtime, ps, card, text)
    for gone in [k for k in _CACHE if k not in seen]:
        del _CACHE[gone]
    return [p for _, ps, _, _ in _CACHE.values() for p in ps]


def card_for(filename: str) -> dict:
    return (_CACHE.get(filename) or (0, [], {}, ""))[2]


def text_for(filename: str) -> str:
    return (_CACHE.get(filename) or (0, [], {}, ""))[3]


# ─── Ranking ──────────────────────────────────────────────────────────────────

def _query(question: str) -> tuple[list[str], dict]:
    """Query terms, and for each the set of synonym stems that also count."""
    q = []
    for w in _words(question):
        if w in _STOP or len(w) < 2:
            continue
        s = _stem(w)
        if s not in q:
            q.append(s)
    alts = {}
    for w in _words(question):
        s = _stem(w)
        if s in q:
            alts[s] = {_stem(x) for x in _SYN.get(w, set()) | _SYN.get(s, set())} - {s}
    return q, alts


def rank(question: str, pool: list[dict], corpus: list[dict], k1: float = 1.2, b: float = 0.75,
         ignore: str = ""):
    """
    BM25 over `pool`, with document frequencies from `corpus` (all summaries),
    so a word common everywhere counts for little even inside one property.
    Returns [(score, coverage, passage)] best first, where coverage is the
    share of the question's words (or a synonym of each) the passage contains.
    """
    q, alts = _query(question)
    # Inside one property, that property's own name is in nearly every passage,
    # so "Who sold us Hopland & Cordova?" matched half its words everywhere and
    # every passage looked like a strong answer. Measured 2026-09-29: 11 of 40
    # rephrased questions came back wrong AND labelled strong. The name picks
    # the summary; it must not also pick the passage.
    drop = set(_terms(ignore))
    q = [t for t in q if t not in drop]
    if not q or not pool:
        return []
    n = len(corpus)
    avg = sum(len(p["terms"]) for p in corpus) / max(1, n)
    df = {}
    for p in corpus:
        for t in set(p["terms"]):
            df[t] = df.get(t, 0) + 1

    def idf(t):
        d = df.get(t, 0)
        return math.log(1 + (n - d + 0.5) / (d + 0.5))

    qwords = [w for w in _words(question) if w not in _STOP and _stem(w) not in drop]
    scored = []
    for p in pool:
        tf = {}
        for t in p["terms"]:
            tf[t] = tf.get(t, 0) + 1
        L = len(p["terms"]) or 1
        score, hit = 0.0, 0
        for t in q:
            best = 0.0
            for cand, weight in [(t, 1.0)] + [(a, 0.6) for a in alts.get(t, ())]:
                f = tf.get(cand, 0)
                if f:
                    s = weight * idf(cand) * f * (k1 + 1) / (f + k1 * (1 - b + b * L / avg))
                    if cand in p["head_terms"]:
                        s *= 1.3
                    best = max(best, s)
            if best:
                hit += 1
            score += best
        # Two question words side by side in the passage: a small phrase bonus.
        low = p["text"].lower()
        for a, c in zip(qwords, qwords[1:]):
            if f"{a} {c}" in low:
                score *= 1.15
        if score > 0:
            scored.append((score, hit / len(q), p))
    scored.sort(key=lambda x: -x[0])
    return scored
