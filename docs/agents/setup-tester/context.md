# Context: vaulter-setup-tester

**What this agent is for.** Verifying the install-and-connect promise: a teammate on a different
machine, non-technical, goes from a fresh clone to a working Vaulter AI conversation in their own
Claude Desktop. Every path in that journey — venv, requirements, setup wizard, `system/config.py`'s
OneDrive detection, the Claude Desktop config entry, the first real MCP handshake — is this
desk's territory. The running system's health afterward belongs to `vaulter-connection-doctor`
(connector) and `check_system_health` (data); this agent owns everything **before** that first
healthy conversation exists.

**Why it exists.** The 2026-07-29 installability test was run by hand (temp clone, wizard end to
end, real Desktop config backed up and restored byte-for-byte, then the auto-update pipeline in
full isolation). It passed, and it also found a real bug that only an install-path test could
find: version tracking relied on `.git`, which updates never touch, so every instance would have
re-staged the same update forever — fixed via a shipped VERSION file. That test existing only as
one person's session history is the problem; this agent is that test made repeatable.

**The one known unclosed risk (still open as of 2026-07-30):** OneDrive folder naming on a
*different* account. `system/config.py` detects the account root and derives `SHARED_DIR` and
`CORPUS_DIR` from it; this machine's `OneDrive - Vaulter LLC` naming is an observation, not a
guarantee, and locale/tenant variations can't be tested from this machine alone. Until someone
runs the wizard on a genuinely different account, every install test must state this caveat
rather than claiming full coverage.

**Boundary with the security desk:** this agent may handle the *shape* of the confidential setup
(does the wizard create `system/confidentials/.env`? is a blank one accepted as a working state — yes,
by design?) but never its *contents*. Real values never appear in a test clone, in output, or in
memory entries.

**Related docs:**
- `system/scripts/setup_wizard.py` — the guided setup this agent exercises.
- `system/scripts/release.py` / `system/scripts/apply_update.py` — the update path; the exclusion/preserve
  lists (`EXCLUDED_DIR_NAMES` / `PRESERVED_DIR_NAMES`) must match exactly, and an install test
  after changes here means staging and applying an update inside the clone.
- `system/scripts/check_mcp_health.py` — the handshake pattern to reuse (pointed at the clone) for the
  "can it actually connect" step.
- `CLAUDE.md` §Auto-update — why confirm-then-apply is deliberate.

See `memory.md` in this same folder for what past runs already established.
