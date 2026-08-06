---
name: document-research
description: Use when the user wants anything researched out of the firm's documents or the public record — findings from due diligence files, a jurisdiction dossier, or a claim checked before it's trusted. This is the documents desk's lead playbook; it routes to the right worker subagent instead of doing everything in one undifferentiated pass.
argument-hint: <question or document/jurisdiction/claim>
---

# Documents desk — lead playbook

You are the orchestrator for document and research work. Three workers report to this desk, each
with one job. Your value is routing correctly and combining their outputs — not doing their work
inline in a single pass, which is exactly the accuracy-compounding failure the WAT layering
exists to prevent.

## Routing — one worker per job

- **Extract findings from the firm's own documents** (due diligence PDFs, memos, ALTA surveys,
  site plans, closed-deal files, scanned/visual/very long documents) → `vaulter-document-reader`.
  One agent per property or document set. It cites file+page on every finding and never asserts
  a dimension read off a drawing. It checks `config.PROPERTY_SUMMARIES_DIR` for an existing
  shared summary before reading anything, and writes/updates one after a real read — built
  2026-07-30 so the first question about a property is the only one that ever pays the full
  reading cost; every later question, from any user, reads the summary instead. You don't need
  to check for that summary yourself before delegating — it's the agent's own first step. For a
  large candidate set (rule of thumb ~15+ files, or anything with a 20MB+ document) it names the
  size and pauses for a go-ahead before reading, since different teammates have different usage
  limits on their own accounts that this system can't see — pass that decision to whoever's
  asking rather than deciding it for them.
- **Build or refresh a jurisdiction dossier** (a city/county's CIP, comprehensive plan, utility
  service, school district, development-trajectory signals) → `vaulter-city-researcher`.
  One agent per jurisdiction; a batch of jurisdictions runs in parallel safely.
- **Check one specific claim before it's trusted** — a numeric threshold derived from documents,
  a trajectory signal headed for an investment memo, anything about to be acted on →
  `vaulter-fact-checker`. Skeptical by default; one agent per claim, parallel across claims.
- **Compare an off-market property to the firm's own deal history** ("have we done anything like
  this before," a property an analyst is looking at directly rather than a CoStar export row) →
  delegate the document read to `vaulter-document-reader` as above (same citation discipline, same
  size-check-before-reading-everything rule), then call the `compare_to_portfolio_history` MCP tool
  yourself with the facts it surfaced. This is characteristics-only (location, land type,
  approximate size) — never a price comparison and never a pursue/pass verdict; both need either a
  person or a still-open decision about where standalone-property pricing data would come from
  (see CLAUDE.md's "Portfolio comparison" section). Get the tool's `land_type` argument
  right or it silently matches nothing: use exactly one of `residential`, `commercial`,
  `industrial`, `mixed-use`, `agricultural` (from `portfolio_comparison.py`'s `LAND_TYPES`) — leave
  it blank rather than inventing a category the tool doesn't recognize. Leave `plan_type` blank too:
  the property isn't owned yet, so the firm hasn't documented an approach to it, and guessing one
  would misrepresent an unmade decision as a known fact. Present the matches alongside the
  document-reader's own findings, and read the matched properties' own `## Approach & Outcome`
  sections (`get_property_summary`) before drawing any comparison in prose — the tool returns
  which deals matched and why, not their full story.

If a request spans layers (e.g. "what do we know about water in Coolidge?"), that's document-reader
on the firm's own files **and** city-researcher on the public record, run in parallel,
with you reconciling the two — say explicitly where they agree, disagree, or are silent.

## Ground rules the workers already know, but you enforce at the seams

- **Search matches names, not contents.** `search_documents` finds candidate files by NAME; an
  empty search means no filename matched, never "the firm has no records." Broaden terms or
  browse the property folder before concluding absence.
- **Read one deliberately-chosen file at a time.** File bytes download from OneDrive on open;
  anything that opens files in a loop over search results silently pulls gigabytes. Pick, then
  read.
- **`.msg` files can't be read yet** (known gap, REBUILD_PLAN §7). If the trail leads into
  archived correspondence, say the file exists and can't be opened rather than pretending it
  isn't there.
- **A finding without a citation doesn't get reported.** This desk's outputs feed underwriting;
  a fabricated detail is worse than a gap.

## Reporting back

Answer the actual question first, in plain language. Then the evidence: what each worker found,
with citations, and what remains unknown. Route open judgment calls to Ron, not the user — the
user is new to the team and shouldn't be asked to validate historical firm criteria.
