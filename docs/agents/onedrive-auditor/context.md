# Context: vaulter-onedrive-auditor

**What this agent is for.** Auditing `config.SHARED_DIR` ("Vaulter AI Shared") — the one OneDrive
folder every teammate's instance writes into — for the accumulation pattern found and fixed
2026-07-29: a tool wrote a brand-new timestamped file on every run, with nothing ever cleaning up
the old ones, so the team-shared folder grew forever with results nobody needed twice.

**Why it exists.** `pipeline/proximity_tool.py` was found writing `<name>_<timestamp>.xlsx`/`.csv`
on every call — re-running it on a property you'd already checked just added another pair instead
of replacing the old one. 32 files had accumulated, several pairs byte-identical re-runs minutes
apart. Fixed to overwrite one file per property instead. The same day, `screening_output/` was
found to have the identical pattern at far larger scale (149 files) via `fit_screen.py`/`report.py`
timestamping every screen the same way — **not yet fixed at the time this agent was written**, so
check whether it still needs fixing or whether someone already applied the same treatment.

**This agent proposes, it never deletes.** `Vaulter AI Shared` is genuinely shared — one person's
cleanup mistake is everyone's data loss, propagated by OneDrive to every teammate's machine. The
existing `cleanup` skill treats this whole folder as a hard stop for exactly that reason. This
agent's job is the opposite of avoidance: go look, deliberately, but report findings for a human
to act on rather than deleting anything itself.

**Not every growing file count is the bug.** `geo_cache/` is a legitimate cache — more area
coverage should mean more entries, not duplicates of the same lookup. Don't flag normal cache
growth; only flag genuinely stale or duplicate-content entries within it. `updates/` and
`org_settings/` were empty as of 2026-07-29 (never exercised for real yet) — check whether the
same "old zip never cleaned up after a newer version is released" risk in `scripts/release.py`
has since become real. `property_summaries/` (added 2026-07-30) is also expected to grow — one
file per property that's ever been researched, named `<slug>.md`. That's normal; the bug pattern
to actually check for here is a property with **more than one** file (a slug-generation mismatch
producing two summaries for the same property instead of one merged file), not the folder having
many files overall.

**Related docs:**
- `CLAUDE.md` — `config.py`'s `SHARED_DIR`/`CORPUS_DIR` distinction; `SHARED_DIR` is written to,
  `CORPUS_DIR` is read-only and out of scope for this agent entirely
- `.claude/skills/cleanup/SKILL.md` — the general repo-cleanup skill; note where its posture
  differs from this agent's (it avoids `SHARED_DIR` outright; this agent's whole job is that folder)

See `memory.md` in this same folder for what past audits already found.
