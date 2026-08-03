# Context: vaulter-screening-checker

**What this agent is for.** Checking that `fit_screen.py` read one specific CoStar/broker export
correctly and ranked it fairly, before its output is trusted or acted on. Not a code reviewer —
a file-specific auditor.

**Why it exists.** Every real bug this screener has had followed the same shape: a new export
had a column layout nobody had tested, and something silently misread it. Reviewing the screener
by asking "does anything look wrong?" kept finding one issue, fixing it, then finding the next —
no convergence. This agent exists to run the same complete checklist every time instead, so
nothing is rediscovered by accident.

**What "fair for any market" means here.** Almost all of Vaulter's own cost/timing/return
evidence is Arizona (see `docs/PORTFOLIO_STANDARD.md`). A file from Texas, Colorado, or anywhere
else must still be scored honestly — not penalized for lacking evidence Arizona happens to have,
and not silently scored using an Arizona-specific number (e.g. Pinal County's $70–99k/acre
horizontal cost) that has no business touching another state's file.

**Source of truth for the checklist.** The checklist embedded in this agent's own instructions
(`.claude/agents/vaulter-screening-checker.md`) is derived from the screening section of the project's
`CLAUDE.md` and `docs/REBUILD_PLAN.md` §3.3. If a genuinely new failure mode is found that isn't
already on the checklist, add it there — that file is the checklist, this file is background.

**Related docs, for deeper context if a finding needs it:**
- `CLAUDE.md` — the screener's four non-negotiable rules and the measured bugs behind them
- `docs/REBUILD_PLAN.md` §3, §3.3 — why the screener was rebuilt, the header-row/column-resolution
  fixes and what broke before them
- `docs/PORTFOLIO_STANDARD.md` §8 — what evidence does and doesn't exist per state
- `system/scripts/check_screener.py` — the automated regression suite this agent runs as step 0

See `memory.md` in this same folder for the log of what past runs actually found.
