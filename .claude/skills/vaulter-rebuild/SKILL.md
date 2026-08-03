---
name: vaulter-rebuild
description: Use when working on the Vaulter AI rebuild described in docs/REBUILD_PLAN.md — deriving the buy-box standard, building jurisdiction dossiers, swapping the geo stack off Google API keys, or moving screening Phase 3 in-conversation. Encodes what to delegate to which subagent, and the safety gates.
---

# Vaulter Rebuild Orchestration

You are the orchestrator. Read `docs/REBUILD_PLAN.md` first — it holds the architecture,
the MCP inventory, and the numbered blockers. This skill covers only *how to run the work*.

## Status — the old hard gate is retired

This skill used to say "delete nothing until the M365/Teams connector is confirmed available."
**That gate is resolved: connectors were ruled out, and the deletions happened anyway** — the
OneDrive-synced library made them possible without a connector. `ingestion/`, `rag_engine.py`,
`email_reader.py`, `outlook_auth.py`, ChromaDB, the scrapers, and the scheduler are all gone.
See `docs/REBUILD_PLAN.md` §0.

Don't re-apply the old gate, and don't propose reviving those modules.

## What's built vs. what's left

**Built** (REBUILD_PLAN §§1–2): the `system/corpus/` document layer, `system/portfolio.py`, the slimmed MCP
tool surface, the keyless geo swap, zero required API keys.

**Left** (§§4–7): the buy-box standard as a readable document (Phase 0), Tier A/B/C area
intelligence, `.msg` support, and Phase 3 moving in-conversation.

Key fact to keep in mind: the firm's library is **already a synced local folder**
(`config.CORPUS_DIR`), ~493k files, searched by filename via a SQLite index. Reading it needs
no connector and never did.

## Delegation map

| Task | Agent | Notes |
|---|---|---|
| Read portfolio docs, extract findings | `vaulter-document-reader` | Fan out one per document or property folder. Handles scanned/visual/long docs. |
| Build a jurisdiction dossier | `vaulter-city-researcher` | One per jurisdiction — **parallelize freely**, they're fully independent. |
| Check a derived number or cited signal | `vaulter-fact-checker` | One per claim. See the mandatory rule below. |
| Code changes (geo swap, Phase 3 move) | do it inline | These are small and localized; a subagent adds overhead without isolation benefit. |

## The mandatory rule: verify before ratifying

Any **number** derived from prose documents, and any **trajectory signal** headed for an
investment memo, goes through `vaulter-fact-checker` before it's treated as true. This is not
optional ceremony — it's the structural answer to the plan's two stated invisible-failure modes
(a wrong disqualifier silently killing deal flow; a fabricated CIP project in a memo).

Fan out one verifier per claim in parallel, then:

- **CONFIRMED** → may become a disqualifier, pending human ratification.
- **INFERRED** → may become a *preference* (weighted, never eliminating). Never a disqualifier.
- **REFUTED / UNVERIFIABLE** → drop it, and report that you dropped it and why.

Never silently discard a refuted claim — the fact that a plausible-sounding threshold failed
verification is itself useful information for whoever ratifies the standard.

## Phase 0 — a first draft already exists

**`docs/COMPANY_PROFILE.md` is written.** Don't start it over. It was derived on 2026-07-27 from
firm-wide templates, five deals analysed in depth, the 2023 underwriting models, and a full
inventory of ~57 active holdings / ~137 exits. Every claim carries a citation, and §8 lists what
could not be established.

Settled, so don't re-investigate:
- The library structure is `!PROPERTIES/<STATE>/<Property>/00. Pre-Acquisition … 06. Disposition`,
  under `Vaulter LLC - shaw`. Old blocker #5 (unconfirmed folder name) is closed.
- The firm has **no written buy box** — criteria were tacit. The Property Analysis form in
  `01. Legal\Entity\Investors\Property Analysis\` is the closest thing, inconsistently filled.
- Deriving numbers from prose is the risky part; §8 of the profile records what stayed unverified.

What's actually left on Phase 0:
1. **Human ratification.** The profile is marked draft/unverified throughout and is built for a
   senior person to correct, not approve. See [[user-new-to-team]] — the user cannot validate
   historical firm criteria themselves.
2. Fan out `vaulter-fact-checker` over any numeric criterion before it hardens into a rule.
3. Keep the **disqualifiers vs preferences** split — the halves fail differently. A wrong
   disqualifier silently destroys deal flow; a wrong preference just misranks.

If extending the evidence base, `vaulter-document-reader` over more closed-deal files is the way —
but the patterns were already consistent across five deals from three eras, so expect
diminishing returns versus getting a human to correct the draft.

## Reporting back

Always surface: what was verified vs. assumed, what was dropped and why, and which gaps remain.
The plan's whole premise is that the standard becomes *readable and challengeable* — so hiding
uncertainty defeats the point.
