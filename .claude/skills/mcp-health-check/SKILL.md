---
name: mcp-health-check
description: Use to check whether the Vaulter AI MCP connector itself is running smoothly -- not the data behind it, the server process. Invoke when the user asks to check the connector/server directly, or any vaulter_ai tool call has just errored, timed out, or hung.
---

# MCP connector health check — orchestrator

You are the orchestrator. This exists so a broken connector gets diagnosed and, where possible,
fixed the same way every time, instead of a one-off "huh, that's weird" followed by nothing.

## Steps

1. **Delegate to `vaulter-mcp-doctor`.** Don't run `scripts/check_mcp_health.py` yourself first --
   the subagent runs it as its own first step, and delegating keeps its context (and any fix it
   makes) isolated from this conversation.
2. **If it reports PASS**, tell the user briefly and move on. Don't pad this out.
3. **If it reports a fix**, tell the user plainly what was actually wrong and what changed --
   this is the record that stops the same bug from being silently rediscovered later. If the fix
   touched `mcp_server.py` or anything it imports, remind the user that Claude Desktop needs a
   restart to load it -- an already-running server doesn't reload edited code.
4. **If it reports an environment issue it couldn't fix** (OneDrive not signed in, no portfolio
   file, etc.), pass that along in plain English -- this is exactly what `check_system_health`
   already surfaces, so it should sound consistent with that, not like a second unrelated warning.

## What this is not

- **Not a replacement for `check_system_health`.** That tool covers whether the *data* behind a
  working connector looks right (corpus synced, portfolio file present). This skill covers whether
  the *connector* is reachable and fast at all -- a different failure mode, checked a different
  way (a real subprocess handshake, not just reading tool output).
- **Not something the user needs to remember to run.** `mcp_server.py`'s own MCP `instructions`
  string already tells Claude to invoke `vaulter-mcp-doctor` automatically the moment any
  vaulter_ai tool call errors or hangs, in any conversation, for any teammate running their own
  instance -- this skill is the explicit on-demand path for "check it right now," not the only
  path to a fix.
