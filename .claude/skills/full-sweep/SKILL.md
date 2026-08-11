---
name: full-sweep
description: Use to run a whole-system health and correctness audit of Vaulter AI -- screening correctness, report readability, confidential-data leaks, shared-folder hygiene, and whether a fresh teammate could still install it. Invoke when the user asks to "check everything", before a release that matters, after a batch of changes, or on a periodic cadence (monthly is the intent).
---

# Full sweep — orchestrator

You are the orchestrator. This exists because a one-off "does everything look okay?" pass
reliably misses things, and a structured fan-out reliably does not. The first run of this
sweep (2026-08-11) found a **live crash** that would have hit any teammate whose export had a
blank county cell, and a **live confidentiality leak** in the public repo. Neither was visible
to casual inspection; both were found by a specialist told exactly what to attack.

## Before you start

Run the deterministic checks yourself. They are fast, and a failure here means stop and fix
before spending agent time:

```
python system/scripts/check_screener.py "<a real CoStar export>"
python system/scripts/check_portfolio_comparison.py
python system/scripts/check_mcp_health.py
```

Then find a real export to hand the agents — `config.COSTAR_DROP_DIR` first, then
`system/data/drop/`. If there is genuinely no export on the machine, say so; several checks
below are meaningless without one and you should not fabricate a file.

## Dispatch these five, in parallel, in ONE message

They own disjoint areas and must not be serialised. Brief each with **what changed since the
last sweep** — an agent told "check everything" returns platitudes; one told "this specific
thing changed yesterday, attack it" returns findings.

| Agent | What it owns |
|---|---|
| `vaulter-screening-checker` | Was the export read correctly and ranked fairly? Prove ranking is unaffected by anything informational (comparisons, cautions) by running with and without and diffing scores row-for-row. |
| `vaulter-report-checker` | Is the generated HTML report factually right, and genuinely readable by a partner who was not in the room? |
| `vaulter-leak-guard` | Real firm data in the public repo — tracked files **and commit messages** and git history. `.gitignore` coverage. What the pattern list would still miss. |
| `vaulter-onedrive-auditor` | Shared-folder accumulation, misfiled items, stray temp files, and whether real-data files match their backups. |
| `vaulter-setup-tester` | Could a brand-new teammate install from the current zip and connect? Dependency pins especially. |

**Constraints every agent must be given, without exception:**

- Do **not** write into the OneDrive shared folder (`Vaulter AI Shared`) — it syncs to the whole
  team. Read-only; propose, never delete.
- Do **not** modify the live install (`C:\Users\<user>\Vaulter AI`) or the real Claude Desktop
  config.
- Fix real bugs in the **dev repo only**, and report exactly what changed.

## When they report back

**Verify before you relay.** Agents are confident narrators and are sometimes wrong — on the
first run one reported a backup file "not found" and was right, while another's framing of a
known-and-deferred item as a new finding was not. Check any claim you are about to act on or
repeat, especially a claimed failure. This costs one command and has already prevented one
false alarm.

Then:

1. **Fix what's cheap and clear-cut** — a missing pattern, a crash with a known shape, a
   cosmetic string. Add a regression check for anything that was a real defect, so the suite
   grows every sweep. It went 71 → 77 on the first run.
2. **Surface what needs a human** — anything destructive (rewriting published history),
   anything that trades confidentiality against usability, anything about firm history only a
   partner can settle. Present the trade, recommend, and stop.
3. **Ship** — full regression, then `release.py` + `--promote` so existing installs update
   themselves, then `build_handoff.py` so the zip for new teammates is current. A fix that is
   only in the dev repo has reached nobody.

## What "passing" means

Not "no agent complained." It means: every deterministic suite green, every agent's claimed
failure either fixed or explicitly accepted with a reason, and the published version actually
carrying the fixes — verified by reading `system/VERSION` on disk, not by trusting a tool's
own success message.
