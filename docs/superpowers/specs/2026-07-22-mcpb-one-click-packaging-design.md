# Design: One-click Claude Desktop Extension (.mcpb) packaging

**Date:** 2026-07-22
**Status:** Proposed — awaiting review
**Part of:** Multi-user onboarding improvements (see `docs/MULTI_USER_TRANSITION.md`, Priority 3).
Step 2 of the two-step plan agreed with the project owner: shrink the install footprint first (see
`2026-07-22-embedding-footprint-shrink-design.md`, already implemented and merged), then package
the result as a one-click install. This design covers step 2.

## Problem

Even with the footprint-shrink work done and the double-click launcher scripts already shipped
(`Setup Vaulter AI.command`/`.bat`), onboarding a new staff member still involves several manual
steps: installing Python themselves first, running the setup wizard, and (for anyone needing
scanned-PDF support) separately installing Tesseract and Poppler. The goal stated by the project
owner is for a non-technical staff member to use Claude Desktop exactly as they always do, and add
this project as a connector the same way they'd add any other extension — nothing terminal-shaped,
nothing manual beyond signing into their own Microsoft account.

Claude Desktop supports exactly this pattern: **Desktop Extensions**, packaged as `.mcpb` files — a
zip archive containing a local MCP server plus a `manifest.json`. A user downloads one file,
double-clicks it (or drags it into Claude Desktop, or installs it from Settings), and Claude
Desktop's own native install dialog handles the rest.

## Facts established before writing this design (not assumed)

Researched directly against the actual `.mcpb` specification (`github.com/modelcontextprotocol/mcpb`,
its `MANIFEST.md`, and `claude.com/docs/connectors/building/mcpb`) before committing to this
approach:

- **Python is explicitly a second-class citizen in this ecosystem.** The docs directly recommend
  Node.js over Python: "Cannot portably bundle compiled dependencies (e.g., pydantic, which the MCP
  Python SDK requires)" under the traditional Python-bundling mode (`server.type = "python"`,
  vendoring a `server/lib/` or `server/venv/` directory into the package).
- **A newer mode solves this:** `server.type = "uv"`. Instead of bundling packages, the manifest
  ships a `pyproject.toml`; the host application (via `uv`, a Python packaging tool) installs Python
  and every dependency **fresh on the user's own machine at install time** — explicitly documented
  as "Handles compiled dependencies (pydantic, numpy, etc.)" and producing a small bundle (~100KB)
  since no packages are vendored. This is the mode this design uses. It requires internet access at
  install time to resolve packages — already an existing assumption of this project (Outlook,
  Anthropic, and Google API calls all need it).
- **The format has zero story for non-Python external binaries.** Confirmed directly: "The
  documentation does not address OCR tool integration or external binary packaging strategies." This
  project depends on two such binaries — Tesseract and Poppler — for scanned-PDF support, and no
  packaging mode in the spec accounts for them. This is why the design below solves OCR installation
  with ordinary application code (a first-run auto-install step) rather than via the packaging
  format itself.
- **Whether arbitrary extra files can ride inside a `.mcpb` archive is unconfirmed.** The spec is
  silent, not permissive — "The specification does not explicitly restrict or permit arbitrary extra
  files." This is exactly why the design does not attempt to bundle Tesseract/Poppler's binaries
  *inside* the package (Approach A, considered and rejected) — that would bet the whole design on an
  undocumented capability, discoverable only by trial and error. The chosen approach (C, below)
  avoids this entirely.
- **Background threads (a continuously-running file watcher + job scheduler, not just responding to
  on-demand tool calls) are neither confirmed nor denied by the spec** — it's silent, framed around
  "responding to MCP tool calls." Since packaging only changes how the same process gets launched
  (still via stdio, still the same `python main.py mcp` entry point underneath), there's no
  documented reason this should behave differently than today's direct-config-file approach — but
  this is a real, unverified assumption that implementation must confirm early, since it's
  foundational to this project's entire architecture.
- **`user_config` exists for collecting values from the user through a generated settings UI**,
  including a `"sensitive": true` flag for masked/securely-stored fields (substituted into
  `mcp_config` via `${user_config.KEY}`). This project's actual secrets (Outlook client ID,
  Anthropic key, Google keys) are **organization-wide, not per-user** — baked in once by whoever
  manages releases, exactly like `confidentials/.env.template` today. Rather than depend on an
  unconfirmed "pre-fill a default value" capability of `user_config`, this design embeds those
  values directly as literal `mcp_config.env` values in the manifest at build time (a step only the
  release-manager runs, mirroring `release.py`'s own trust model) — `user_config` is not used at all
  for organization-wide secrets. The one genuinely personal step, Outlook's device-code sign-in,
  is unaffected by any of this and works exactly as it does today.
- **Platform-specific launch configuration is supported** via `platform_overrides` inside
  `mcp_config` (confirmed for `win32`/`darwin`) — not needed for this design specifically (there is
  only one target platform), but confirms the packaging format is Windows-aware if ever needed.
- **Installation is per-user, three ways** (double-click, drag-and-drop, or Settings → Extensions →
  Install Extension), all opening the same native install/review dialog. No terminal at any point.

## What's being proposed

### 1. Package as a `.mcpb` using `uv` mode

Build `manifest.json` with `server.type = "uv"` and a `pyproject.toml` listing this project's
dependencies (the same list currently in `requirements.txt`, translated to `pyproject.toml`'s
format). Claude Desktop's own `uv`-based runtime resolves and installs Python plus every dependency
on first install — no system Python, no manual `pip install`, no separate setup wizard step. This
supersedes the "Setup Vaulter AI" launcher/wizard as the primary onboarding path (see below for what
happens to the existing ones).

### 2. Organization-wide credentials baked in at build time, not prompted per-user

`mcp_config.env` in the manifest carries the real `OUTLOOK_CLIENT_ID`, `OUTLOOK_TENANT_ID`,
`ANTHROPIC_API_KEY`, `GOOGLE_PLACES_API_KEY`, and `GOOGLE_MAPS_API_KEY` values directly, filled in
once by whoever packages a release (never seen by staff, never typed by staff) — mirroring
`confidentials/.env.template`'s existing "fill in once, distribute" model. No `user_config` fields
are defined for these. The only step a new user ever performs themselves is signing into their own
Microsoft account via the existing device-code flow (`python main.py auth` today, or an in-chat
equivalent — unchanged by this design either way).

### 3. OCR tools install themselves silently on first run

A new startup step (running in its own background thread, alongside the existing watcher and
scheduler threads, so it never blocks the MCP stdio connection from becoming available) checks —
using `config.py`'s already-existing auto-detection (`_find_executable`/`_find_poppler_bin_dir`) —
whether Tesseract and Poppler are present. If not:
- **Tesseract:** downloads the official UB-Mannheim Windows installer and runs it with silent,
  per-user (non-admin) install flags.
- **Poppler:** downloads the official Windows release zip and extracts it to a per-user local
  folder (it's not an installer at all, just files — no elevation needed either way).
- Updates the detected paths so the rest of the app picks them up immediately, without a restart.
- Runs once (a marker file records success so this doesn't repeat on every startup), and degrades
  exactly like today's manual-install messaging if the download/install ever fails — never a hard
  crash, matching this project's existing "missing optional capability degrades gracefully"
  convention.

### 4. The existing wizard/launchers are kept as a fallback, not deleted

`setup_wizard.py`, `Setup Vaulter AI.command/.bat`, and `Sign In to Outlook.command/.bat` remain in
the repo exactly as they are today — for troubleshooting, for anyone who can't or doesn't want to
use the `.mcpb` path, and as the officially-supported manual alternative. This mirrors
`apply_update.py`'s own existing pattern (`python apply_update.py` stays a manual/troubleshooting
CLI entry point even though the everyday path is now conversational).

### 5. Auto-update overlap is an open question, deliberately not pre-decided

Claude Desktop is documented to apply "automatic updates" to installed extensions, but the spec
gives no detail on how a developer controls this. This project already has its own complete,
human-approved update mechanism (`release.py` / `apply_update.py` / `apply_pending_update`, Priority
4). Whether `.mcpb`'s own update mechanism can fully replace that, should run alongside it, or
needs to be disabled in favor of the existing one is **not decided in this design** — it needs to be
understood concretely (reading `.mcpb`'s actual update behavior once installed, not just its docs)
during implementation, and reported back before deciding whether to simplify or retire any part of
the existing Priority 4 mechanism. Nothing about Priority 4's mechanism is removed or changed by this
design; this is purely a note to resolve later, not a requirement of this task.

### 6. Implementation happens on Windows, not this development machine

This design was researched and written on a Mac, where the actual "double-click a `.mcpb` file in
Claude Desktop" experience cannot be personally verified end-to-end. The project owner confirmed
every staff machine is Windows, and confirmed they have Claude Code available on their own Windows
machine. **Per the project owner's explicit direction, implementation of this design happens in a
Claude Code session on that Windows machine**, not this one — so the real install experience can be
tested directly and fixed in place, rather than built blind here and manually verified after the
fact. The plan produced from this design should be handed off for execution there.

## What does NOT change

- The MCP server's own tools, business logic, and architecture (`mcp_server.py`, `ingestion/`,
  `pipeline/`, `analysis/`) — packaging changes how the process gets launched and configured, not
  what it does once running.
- The background watcher + scheduler threads — same code, same behavior, launched the same way
  underneath (`python main.py mcp`, just invoked by Claude Desktop's `uv` runtime instead of a
  hand-edited config entry).
- The existing auto-update mechanism (Priority 4) — kept as-is pending the open question in item 5
  above.
- Each staff member's local-only architecture and privacy boundary (per `mcp_server.py`'s own
  header) — `.mcpb` installation is still fully local and per-user; nothing about this design routes
  anything through a shared server.

## Testing plan

Since implementation happens on the actual target platform (Windows, via Claude Code there), testing
should be real, not simulated:
1. Build the `.mcpb` package and confirm `mcpb pack`/its validation step accepts the manifest without
   errors.
2. Do a real double-click install into a real Claude Desktop on Windows and confirm: the install
   dialog appears, dependencies resolve via `uv`, the server starts, and its tools are callable from
   a real conversation.
3. Confirm the background watcher/scheduler threads are actually running post-install (e.g. via
   `check_system_health`) — resolving the open question in the Facts section about whether this
   architecture is unaffected by the new launch method.
4. Confirm the OCR auto-install step actually works on a machine without Tesseract/Poppler already
   present: downloads succeed, silent installer flags work as expected (no UAC prompt, no visible
   installer window), and a scanned-PDF ingestion afterward actually succeeds.
5. Confirm the org-wide credentials baked into the manifest are actually present in the running
   server's environment (e.g. Outlook device-code flow reaches the right tenant) without ever having
   been typed by the person testing.
6. Confirm re-installing/updating the extension doesn't re-trigger the OCR auto-install unnecessarily
   (the marker-file check from item 3 of the design).

## Out of scope for this design

- Actually resolving the auto-update overlap question (item 5) — investigate and report during
  implementation, decide separately afterward.
- Any change to Outlook's own sign-in mechanism.
- Meeting-transcript search (Part F) or shared portfolio documents (Part G) from the broader
  roadmap — unrelated, separate future work.
- Submitting this extension to Anthropic's public Connectors Directory — this is a private,
  internal-team tool, distributed only within the company, not publicly listed.
