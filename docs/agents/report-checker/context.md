# Context: vaulter-report-checker

**What this agent is for.** Checking that a generated screening HTML report is factually complete
against the screen it was built from, and genuinely readable by a non-technical partner — before
it's shared outside the conversation that produced it.

**Why it exists.** The report is the thing a partner actually sees; the ranking arithmetic behind
it doesn't matter if the report renders blank fields or reads like it was written for a developer.
The previous dashboard (`dashboard_server.py`, retired) failed exactly this way once — it read
sheet names the screener no longer wrote and displayed nothing, silently. This agent exists so
that failure mode gets caught before a report goes out, not after.

**Two separate jobs, don't conflate them:**
1. **Correctness** — does the report match what the screen actually found. A missing candidate or
   a blank field is a correctness bug.
2. **Readability** — can a non-technical partner actually understand it. Unexplained jargon or a
   buried caveat is a readability bug, not a correctness bug, and should be reported as such.

**Related docs:**
- `CLAUDE.md` — the report's three-reader-layer design (decision → shortlist/map → full detail)
  and why `dashboard_server.py` was retired
- `docs/REBUILD_PLAN.md` §3.2 — what `report.py` replaced and why

See `memory.md` in this same folder for the log of what past runs actually found.
