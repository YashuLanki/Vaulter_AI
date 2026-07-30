# Context: vaulter-mcp-doctor

**What this agent is for.** Checking the MCP *connector itself* -- the `mcp_server.py` process
launched by `python main.py mcp` -- is it reachable, does it register all its tools, does every
call return promptly. This is a different layer from `check_system_health`, which reports whether
the *data behind* a healthy connector is in good shape (corpus synced, portfolio file present,
etc.). A connector can be perfectly wired and still report data problems; it can also look "fine"
by every data measure while itself hanging or crashing. This agent is the latter.

**Why it exists.** Built 2026-07-30, immediately after finding and fixing a real bug:
`check_system_health` was hanging 60-240+ seconds -- long enough that Claude Desktop's own client
gave up and reported `MCP error -32001: Request timed out`, making a live, working server look
dead. The root cause (a stuck git subprocess inside `_get_code_version()`, not OneDrive as the
code's own comments assumed) was only found by driving a *real* stdio subprocess and capturing a
`faulthandler` stack dump mid-hang -- calling the same tool function directly in-process
(bypassing the stdio transport) consistently looked fast and never reproduced it. That distinction
is the whole reason this agent's primary tool, `scripts/check_mcp_health.py`, insists on spawning
a genuine subprocess rather than importing `mcp_server.py` and calling a tool function directly.

**Trigger conditions.** Use whenever:
- Any `mcp__vaulter_ai__*` tool call errors, times out, or otherwise behaves unexpectedly, in any
  user's conversation.
- The user asks to check the connector, or asks something like "is everything running smoothly."
- `check_system_health`'s own `instructions=` text (in `mcp_server.py`'s `create_mcp_server()`)
  tells Claude to invoke this agent automatically -- see that string for the exact wording; it's
  meant to make this proactive for every user without them having to ask.

**This agent fixes real code bugs, unlike the shared-folder/security QA agents which only
propose.** The precedent is `vaulter-screening-qa`/`vaulter-dashboard-qa`, which have Edit access
and have fixed real bugs directly, then re-verified. The line: fix anything that's actually a bug
in this project's own code; report (never attempt to fix) anything that's the local machine's own
environment -- OneDrive not syncing, no portfolio file, network trouble. Editing code cannot fix
someone's OneDrive sign-in.

**A real, unexplained wrinkle worth knowing about, not treated as a bug:** every `python main.py
mcp` launch produces two living processes in a parent-child relationship (confirmed via
`Get-CimInstance Win32_Process` on both a Desktop-launched instance and a freshly test-driven one).
Grepping `main.py`/`mcp_server.py` found no `subprocess`/`sys.executable`/`multiprocessing` call
that would explain a self-respawn -- it looks like a venv/launcher artifact on this machine, not
project code. It's suspected (not proven) to be how the 2026-07-30 git-subprocess pipe ended up
leaking to a sibling process and never seeing EOF. Don't chase this further unless the hang class
recurs after the current fix -- see `memory.md` for the full account.

**Related docs:**
- `CLAUDE.md` -- `mcp_server.py`'s "no background threads" invariant, and the "a crash and a hang
  are indistinguishable from the outside" lesson this agent exists to catch before a user does.
- `scripts/check_mcp_health.py` -- the deterministic tool this agent runs first, every time.

See `memory.md` in this same folder for what past runs already found and fixed.
