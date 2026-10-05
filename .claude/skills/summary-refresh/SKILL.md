---
name: summary-refresh
description: Use to bring a property's shared write-up up to date from documents filed since it was last written — whether the health check flagged it, someone asked about that property, or a batch needs catching up. Orchestrates the reading, the writing and the checks afterwards; the reading itself belongs to vaulter-document-reader.
---

# Bringing a property summary up to date — orchestrator

The health check flags active-stage properties whose documents are newer than their write-up.
That flag is a prompt, not a job queue: it says *something arrived*, not *something changed*.
Your job is to find out which, and to leave the team's record better than you found it.

**The reading is not yours.** `vaulter-document-reader` knows how to handle scanned pages,
drawings, 200-page files and sticky notes, and how to cite. This file is about what happens
around that: deciding what is worth reading, getting the writing into the right file, and
proving afterwards that nothing was lost.

---

## Steps

### 1. Check the file list is current before believing anything

If the list of documents is days old, every answer you give about what exists inherits that
staleness. **This is the project's oldest mistake**: a currency check once reported "no
documents newer than the 3rd" as fact when there were 57, because the list it asked was eight
days old.

`check_system_health` states the list's age. If it is stale, rebuild it first, or say plainly
that your answer is only as fresh as the list.

### 2. Find what is actually new, and name it

Get the filenames, not a count. **A count cannot be trusted:** OneDrive rewrites a file's
modified date when it re-syncs, so on one real property years of old documents all looked like
they had arrived that year. The filenames carry the true dates in their own `YYMMDD` prefix.

So: read the names and judge from them. A quarterly report that has just been renamed is not
news. An executed amendment is.

### 3. Decide what to read — not everything

Reading is where the cost is. Pick the documents whose names suggest the deal's STATUS may
have moved: amendments, executed agreements, letters of intent, closing statements, loan
modifications, terminations. Skip what the summary already covers.

**Never open documents in a loop.** Names are free; opening a file downloads it. A loop over
search results silently pulls gigabytes through OneDrive.

### 4. Dispatch the reader

One `vaulter-document-reader` per property. Parallel across different properties is fine —
they touch different files.

Tell it which documents you want read and what question you are trying to settle. A reader
given a property and no question reads for "what is this deal", which is the wrong pass when
you are asking "what changed".

### 5. Write the update — one property at a time, yourself

**For a single property**, letting the reader write its own summary is fine.

**For a batch, have the readers REPORT and write each one in yourself, one at a time.** This is
not fussiness: during a 29-property refresh, the first such write was caught landing a
Phase 2-4 update into the Phase 1 property's file — and moving that file's freshness date
*backwards* — precisely because the main session was writing them in one by one and looked.
Parallel writers would have scattered that silently across several files.

Use `update_property_summary`. It appends a dated section, refreshes the card's dates, and
inserts above the Sources list.

---

## Check every write before moving on

Four things, each of which has gone wrong at least once:

1. **It landed in the right file.** A longer property name could resolve to a shorter
   property's summary. Confirm the file you meant is the file that changed.
2. **Every original heading survives.** Compare the set of headings before and after; the new
   set must contain the old one.
3. **Every citation resolves** against the current file list. A citation nobody can follow is
   worse than no citation, because it reads as evidence.
4. **The freshness date moved forward, never back**, and the staleness flag now clears for
   that property.

**Back up the summary before writing**, and afterwards confirm the line count only grew. That
single check has caught a silent loss of content that nothing else would have.

---

## What to distrust

- **A reviewer's sticky note is not the document's own words.** Beside a "$530 per square
  foot" line, a reviewer had written only `$5.30?`. That is a doubt, not a correction, and
  quoting it as the document's figure would be exactly the confident-wrong answer this project
  removes everywhere else. Comments carry their author and date — **the date is what makes
  them usable**, since on one property the comments predated the summary and the right answer
  was "nothing new here".
- **Comments are easy to miss and have mattered.** Three findings in one afternoon existed
  ONLY as sticky notes — including the sole evidence anywhere that a deal had fallen through.
- **A dimension read off a drawing.** Never assert one.
- **A generic filename.** If the document is called something like `Summary.pdf`, cite it with
  its folder path — there are dozens of files by that name, and a bare citation sends the
  reader to the wrong one. Measured: 21 citations across the summaries are ambiguous this way.
- **Contradicting the existing text.** Where a new document supersedes an old statement,
  replace it and date it — `(was: ...)` — rather than leaving both standing. Where two real
  sources genuinely disagree and neither is disproved, leave both visible and flag it for a
  person.

## Reporting back

Lead with what actually changed about the deal — not how many documents were read. If the
status moved (closed, extended, signed, terminated, a new offer), say that first and cite it.
If nothing material changed, say that plainly: "three documents filed, none of them change the
picture" is a useful answer and a cheap one to trust.

Then say what you did NOT read, and why. The Gaps section exists so a later reader knows the
difference between "checked and absent" and "never looked".
