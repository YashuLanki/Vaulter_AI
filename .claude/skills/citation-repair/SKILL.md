---
name: citation-repair
description: Use when the answer-quality check reports citations that cannot be found, or when documents on the drive have been renamed and the property write-ups still name the old files. Corrects the names in place, never deletes a citation, and refuses to guess between two candidates.
---

# Repairing citations — orchestrator

A citation nobody can follow is worse than no citation, because it still reads as evidence.
This job restores the link between a written finding and the document that proved it.

**It recurs.** The firm renames each quarterly report when its review finishes, so a batch of
citations goes stale every quarter. The write-ups were right when written; the drive moved.

**The rule that governs everything here: fix the name, never delete the citation.** A finding
that cites a document a reader cannot open is a problem. A finding with no citation at all is
a worse one, and irreversible — nobody can tell later whether it was ever evidenced.

---

## Steps

### 1. Ask the check, and read its categories carefully

```bash
python system/scripts/check_answers.py
```

It already does the hard separation. **Do not treat its whole list as one problem** — it
reports several different things and they need opposite responses:

| What it says | What it means | What to do |
|---|---|---|
| **renamed** — "is really `<name>`" | The document is still on the drive under a new name | Correct it — mechanical |
| **punctuation** | Differs by a hyphen, a space, a comma | Correct it — mechanical |
| **deliberately shortened** (a wildcard or `...`) | Written that way on purpose | **Leave alone.** Not a failure |
| **a file this system produces** | Not a library document at all | **Leave alone** |
| **unresolved** | Nothing on the drive matches | Needs judgement — see step 4 |

*Why the separation matters:* this check once went red reporting 30 unfindable citations when
**eight were simply renamed and a ninth differed by one missing hyphen**. Reporting those as
fabricated sources was a confident wrong cause — and it **buried the four that were real**.

### 2. Correct the mechanical ones

For each renamed or punctuation case:

- **Take the true spelling from the file list, not from the check's output.** The check prints
  names in lower case for comparison; the summary needs the document's actual casing.
- **Confirm the replacement matches exactly one file.** If two files match, stop — see the
  warning below.
- **Back up every summary before writing.**
- **Replace the name in place.** Nothing else about the line changes.

Refuse the whole run if any single replacement does not land exactly as expected. A partial
pass across the team's curated write-ups is worse than no pass.

### 3. Prove nothing else moved

After writing, for every file touched:

- the **line count is unchanged** — only names were swapped, no content lost
- the **line endings** are what they were
- re-run the check and confirm the number fell

### 4. The unresolved ones — judgement, not automation

These have no match. Search the file list for near misses by shared words, then decide
honestly which case you are in:

- **Obvious.** Every word matches and the real file simply carries the property name as a
  prefix. Correct it.
- **Probable but consequential.** Two candidate documents exist for the same deal — different
  parcels, different dates, a settlement statement beside an affidavit. **Ask a person.** The
  cost of a wrong citation here is someone reading the paperwork for the wrong parcel.
- **Genuinely missing.** Nothing shares more than a word or two. **Leave the citation as
  written.** It is a true record that the document was seen; it just is not in the folder.
  Deleting it would erase the only evidence that it ever existed.

---

## The warning that governs the matching

**A loose match can cite the wrong person's signature.** This library holds six signature pages
of one resolution differing only by the signer's surname. Matching on letters and digits alone,
ignoring punctuation, is safe **only when exactly one real file matches** — measured across
every distinct name on the drive, 0.3% are ambiguous and are refused outright.

Never widen the match to make a stubborn case resolve. A refusal is a correct answer.

---

## The second job: citations that resolve but still point nowhere useful

A citation can pass the check and still be unusable, because the name it gives belongs to
several genuinely different documents. Measured 2026-10-05: of 603 documents cited across all
write-ups, **21 name something generic enough that a reader cannot tell which file is meant** —
one of them shares its name with 59 different documents.

Most are mild: every copy sits inside that property's own folder, so they are drafts of the
same thing. A few are not.

**The fix is to add the folder path to the citation**, which is what the unambiguous ones
already do. **It needs a person who knows the deal** — the duplicates usually sit in the same
folder, so knowing the property does not narrow it. Report these; do not guess between them.

---

## Reporting back

Say how many were corrected, how many were left alone and why. Name the ones a person must
decide, with the candidates, so the decision can be made without repeating the search.

**If the unfindable count fell below the recorded baseline, say so and offer to lower it.** The
rule in this project is to lower a baseline as things get fixed and never raise one to make a
run go green — a baseline left high after a real improvement quietly hides the next regression.
