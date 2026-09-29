"""
Does "find inside one document" return the passage that answers -- and what does it save?

    python system/scripts/check_find.py

Runs system/data/eval/find_questions.json (gitignored: real firm documents and
facts) through the same code find_in_document uses. Reads only documents that
are already on this machine; it downloads nothing and calls no model.

The questions were written by rephrasing each fact the way a teammate asks
("what's the sale price" where the text says "Purchase Price"), so this is a
hard test for word matching on purpose. It reports:

  * FOUND: the answering passage was among those returned.
  * TOKENS: what was returned, against reading the whole document.

A miss is safe only if Claude notices none of the passages answers and falls
back to read_document; that judgement is Claude's, tested by the answer-eval
skill, not here. The found rate is a regression detector, not a standard:
lower BASELINE_FOUND only when a fix earns it, never raise it to pass.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TOP_N = 8
BASELINE_FOUND = 0.60


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def main() -> int:
    import logging
    logging.disable(logging.CRITICAL)
    import config
    from corpus import read_document
    from corpus.index import resolve_in_corpus, is_online_only
    from corpus.find import find

    qfile = Path(config.DATA_DIR) / "eval" / "find_questions.json"
    if not qfile.exists():
        print(f"No question set at {qfile}.")
        return 2
    qs = json.loads(qfile.read_text(encoding="utf-8"))
    texts, found, n, skipped, tok_find, tok_full = {}, 0, 0, 0, 0, 0
    misses = []
    for q in qs:
        if q.get("kind") != "answerable":
            continue
        path = q["path"]
        if path not in texts:
            try:
                if is_online_only(resolve_in_corpus(path)):
                    texts[path] = None          # would download: never, in a check
                else:
                    texts[path] = read_document(path, max_chars=5_000_000)[0]
            except Exception:
                texts[path] = None
        text = texts[path]
        if text is None or _norm(q["answer_phrase"]) not in _norm(text):
            skipped += 1
            continue
        res, _ = find(text, q["question"], TOP_N)
        ok = any(_norm(q["answer_phrase"]) in _norm(p["text"]) for p in res)
        found += ok; n += 1
        tok_find += sum(len(p["text"]) for p in res) // 4 + 120   # + the reply's own framing
        tok_full += len(text) // 4
        if not ok:
            misses.append(q["question"])
    if not n:
        print("Nothing could be checked (documents not on this machine?).")
        return 2
    print("Find inside one document, measured against rephrased questions\n")
    print(f"  The answering passage was among the {TOP_N} returned for {found} of {n}")
    print(f"  Tokens per question: about {tok_find // n} returned, against {tok_full // n} "
          f"to read the whole document ({100 - 100 * tok_find // max(1, tok_full)}% less)")
    miss_rate = (n - found) / n
    blended = tok_find / n + miss_rate * tok_full / n
    print(f"  Counting a full read after every miss: about {int(blended)} tokens per question "
          f"({100 - int(100 * blended / (tok_full / n))}% less than always reading)")
    if skipped:
        print(f"\n  Skipped {skipped} question(s): document not on this machine, or its answer "
              f"phrase no longer matches the text.")
    if misses:
        print(f"\n  Misses ({len(misses)}), candidates for the synonym list:")
        for m in misses:
            print(f"      {m}")
    return 0 if found / n >= BASELINE_FOUND else 1


if __name__ == "__main__":
    sys.exit(main())
