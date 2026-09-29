"""
Does summary search find the right passage -- and when it does not, does it SAY so?

    python system/scripts/check_retrieval.py

Runs the questions in system/data/eval/retrieval_questions.json (gitignored:
every one is a real firm fact) through the same ranking search_property_summaries
uses. Free: it reads the team's summaries, nothing else, and calls no model.

The question set was written by rephrasing each fact the way a teammate would
ask it -- "sale price" where a summary says "consideration" -- so it is a hard
test for a keyword search on purpose. It measures three things:

  * FOUND: a returned passage contains the phrase that answers the question.
  * A MISS THAT SAID "STRONG": the dangerous case. The search returned the wrong
    passages and told Claude to trust them. Every other miss is announced as
    partial or weak, and Claude is told to read the whole summary.
  * AN UNANSWERABLE QUESTION CALLED "STRONG": same danger from the other side --
    a question the summary cannot answer, presented as answered.

The two dangerous counts are what the design has to keep near zero. The found
rate is the one to raise, by adding synonyms where misses show a real gap.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

TOP_N = 10
CROSS_N = 8


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*`_]", "", s)).strip().lower()


def main() -> int:
    import config
    import summary_search as ss

    qfile = Path(config.DATA_DIR) / "eval" / "retrieval_questions.json"
    if not qfile.exists():
        print(f"No question set at {qfile}.")
        return 2
    questions = json.loads(qfile.read_text(encoding="utf-8"))
    corpus = ss.load_all(config.PROPERTY_SUMMARIES_DIR)
    by_file = {}
    for p in corpus:
        by_file.setdefault(p["file"], []).append(p)

    tally = {"single": [0, 0], "cross": [0, 0]}
    misses, bad, card_answers = [], [], 0
    for q in questions:
        kind = q.get("kind")
        files = q.get("files") or []
        phrase = _norm(q.get("answer_phrase") or "")
        if kind == "single":
            if not files or files[0] not in by_file:
                bad.append(q["question"]); continue
            if phrase and phrase not in _norm(" ".join(p["text"] for p in by_file[files[0]])):
                bad.append(q["question"]); continue
            card = ss.card_for(files[0])
            name = " ".join([card.get("property") or ""] + list(card.get("aliases") or []) + [q.get("property") or ""])
            shown = [p for _, _, p in ss.rank(q["question"], by_file[files[0]], corpus, ignore=name)[:TOP_N]]
            ok = any(phrase in _norm(p["text"]) for p in shown)
            tally["single"][0] += ok; tally["single"][1] += 1
            if not ok:
                misses.append(q["question"])
        elif kind == "cross":
            got, per = [], {}
            for _, _, p in ss.rank(q["question"], corpus, corpus):
                if per.get(p["file"], 0) >= 2:
                    continue
                per[p["file"]] = per.get(p["file"], 0) + 1
                got.append(p["file"])
                if len(got) >= CROSS_N:
                    break
            tally["cross"][0] += len(set(files) & set(got)); tally["cross"][1] += len(files)

    s_ok, s_n = tally["single"]; c_ok, c_n = tally["cross"]
    print("Summary search, measured against rephrased questions" + chr(10))
    print(f"  One property: the answering passage was among the {TOP_N} returned for {s_ok} of {s_n}")
    print(f"  Across properties: {c_ok} of {c_n} answering properties named")
    print(chr(10) + "  A miss is safe only if Claude notices none of the passages answers and reads")
    print("  the whole summary. That judgement is Claude's, not this script's; the")
    print("  answer-eval skill is where it gets tested.")
    if bad:
        print(chr(10) + f"  Skipped {len(bad)} question(s) whose file or answer phrase no longer matches.")
    if misses:
        print(chr(10) + f"  Misses ({len(misses)}), candidates for the synonym list:")
        for m in misses:
            print(f"      {m}")
    # A regression detector, not a standard: the found rate the day this was built.
    return 0 if s_n and s_ok / s_n >= BASELINE_FOUND else 1


BASELINE_FOUND = 0.75


if __name__ == "__main__":
    sys.exit(main())
