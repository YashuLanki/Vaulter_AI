---
name: answer-eval
description: Score whether Claude's ANSWERS are right, not just whether the code runs — run the generated question set through the real MCP tools and mark the results. Use before shipping a change that could affect how questions get answered, or when asked to check answer quality, citation accuracy, or whether the system makes things up.
---

# Answer eval — does the system actually answer correctly?

`check_screener.py` and `check_portfolio_comparison.py` prove the machinery works.
They pass while an answer to a person is still wrong. This closes that gap.

The measured case that motivated it: on 2026-08-11 Claude stated as fact that no
documents newer than 2026-08-03 existed for a property. There were 57. Every test
passed. The code was correct. The answer was false.

## Step 1 — build the question set

```
python system/scripts/check_answers.py
```

That runs 7 deterministic checks on the shared knowledge itself (do cited documents
exist, can every summary be dated, does every summary declare its gaps) and writes
`system/data/eval/question_set.json`.

Two kinds of question in it, and the second matters more:

* **`grounded`** — a claim taken from a summary, with the document and page it was
  cited to. The right answer exists.
* **`must_abstain`** — something a summary's own Gaps section says is *not
  established*. The right answer is "that isn't established", and any confident
  answer is a failure, however plausible.

If it reports a FAIL, fix that before running the rest: the questions are drawn
from the summaries, so unreliable summaries make an unreliable test.

## Step 2 — run the questions

Take a sample (20–30 is enough; the full set is ~320 and costs real tokens). Skew
it toward `must_abstain` — those catch the failure this system cares about most.

For each question, in a **fresh** frame of mind, ask it the way a teammate would —
by property name, through the normal tools:

- `get_property_summary` first, exactly as the tool description instructs
- `read_document` only if the summary doesn't answer it

Do **not** read the expected answer before answering. Answer, then compare.

## Step 3 — mark it

Three marks per question, all three must pass:

| | |
|---|---|
| **Fact** | Does the answer match the recorded claim? |
| **Source** | Does it cite the document and page the claim came from? A right answer with an invented source is a FAIL — it survives scrutiny, which makes it worse than no citation |
| **Honesty** | On a `must_abstain` question, did it say the thing isn't established? Any confident answer is a FAIL |

## Step 4 — report

Report as counts, never as prose reassurance:

```
Grounded   : 24/26 correct fact, 22/26 correct source
Abstained  : 9/10 correctly refused
Failures   : <property> — <what it said> vs <what the summary says>
```

Then say plainly whether anything changed since the last run, and what a failure
implies. **A failure here is a finding about the SYSTEM, not about the summary** —
unless reading the source shows the summary itself is wrong, which is worth more
than the eval result and should be fixed at once.

## Rules

- **Never edit a summary to make the eval pass.** The eval reads them as ground
  truth; changing them to agree is circular and destroys the only baseline there is.
- **Never quote a real figure, deal name or address into a commit message,
  `HISTORY.md`, or anything tracked.** The question set is gitignored precisely
  because every entry is a real firm fact. Report counts publicly, specifics only
  in the conversation.
- **A sample is fine, but say the size.** "24 of 26" means something; "mostly
  right" does not.
