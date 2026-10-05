---
name: asking-this-codebase
description: Use before writing a throwaway script to investigate something in Vaulter AI — which property resolves to which folder, whether a citation is real, what the portfolio holds, whether a check is right. Encodes how to ask this codebase its own questions correctly, because the recurring failure here is a hand-written query that is quietly wrong and answers with confidence.
---

# Asking this codebase its own questions

Nearly every wrong answer this project has produced came from the same place: **not broken
code, but a hand-written query that looked reasonable and was wrong.** The system's own
functions were right; the throwaway script asking them was not.

Measured on 2026-10-05, in one session, six separate times:

- `_best_needle(name, None, text)` — passing no database connection, so every count returned
  zero and **all 21 results came back identical**
- passing a summary's filename (`cedar-flat`) where the property's real name
  (`Cedar Flat & Draper`) was needed — the slug has dropped the punctuation the real
  name carries
- comparing rows with `is not` instead of `!=`, so **49 of 49 looked changed** when none were
- `portfolio.load_properties()` unpacked as a list when it returns a tuple
- a half-finished regex matching across a line break and reporting a correct message as missing
- a shell heredoc eating backslashes, writing a broken script four separate times

None of these produced an error. Every one produced a **confident, plausible, wrong number**.

## The rule that catches all of them

**A uniform result is a broken query until proven otherwise.** All zero, all identical, all
missing, all changed — that is the signature of a question asked wrong, not of a system that
is wrong. This project already applies that rule to search results and to map providers; it
applies just as hard to your own scratch script.

The second rule: **prefer the system's own function over your own version of it.** If a
tested function already answers the question, call it. Writing a parallel implementation in a
scratch file means two things answer one question, which is this codebase's most repeated bug.

## Before trusting a scratch script

1. **Run it against a case whose answer you already know.** If it cannot get a known-good
   case right, its verdict on the unknown ones is worth nothing.
2. **Check the shape of what you got back.** A tuple where you expected a list, a dict where
   you expected rows. Print it once before building on it.
3. **If the result is uniform, assume you broke it.** Go back to rule one.
4. **Compare values, not objects.** Most of these readers build a fresh object each call, so
   identity comparison always reports a difference.

## Things that specifically bite here

- **Functions that need a live database connection.** Several take one as an argument and
  silently return zero for everything when it is missing. Pass a real one.
- **Names are not interchangeable.** A property has a name in the project list, a name on the
  drive, a summary filename, and sometimes a second name after a slash or in brackets. The
  system has one resolver for this; use it rather than guessing which spelling to pass.
- **Remembered answers.** Some lookups cache their result. A scratch test may be reading a
  stored answer rather than computing one — including a stored *failure*. Check the cache
  before concluding the logic is broken.
- **Shell heredocs mangle backslashes and escape sequences.** This has silently corrupted
  scripts repeatedly. Write the script to a file with the Write tool instead.
- **Line endings.** Files in this repo are a mix of both. Read the bytes, preserve what you
  find, and check afterwards — a whole-file rewrite hides the real change in the diff.

## Before touching the team's shared files

Property summaries, the portfolio file, anything under the shared folder: **back it up first**,
and after writing, confirm the line count is unchanged unless you meant to change it. That
single check has caught a silent loss of content that nothing else would have.

## What this is not

Not a substitute for the regression suites — those test the system. This is about the scratch
code you write *to investigate* it, which nothing tests at all. That asymmetry is the whole
reason this file exists: the most carefully checked codebase in the project is surrounded by
throwaway scripts that are checked by nobody.
