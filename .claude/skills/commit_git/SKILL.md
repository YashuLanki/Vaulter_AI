---
name: commit_git
description: Use when asked to commit and push changes to GitHub, or when the user invokes /commit_git. Reviews what actually changed, stages it deliberately, drafts a commit message in this repo's own style, commits, and pushes to the current branch's tracked remote.
argument-hint: [optional commit message override]
---

# Commit and push

Invoking this skill **is** the user's authorization to push — that's the whole point of it
existing, so don't ask "should I push?" every time the way a one-off request would. It still
stops and asks, every time, if something below looks wrong rather than pushing through it.

## 1. Look before touching anything

`git status` and `git diff` first, always — never stage or commit blind. Confirm this is the
expected repo (`origin` → `github.com/YashuLanki/Vaulter_AI`) and note the current branch; if
either is unexpected, stop and ask rather than assume.

## 2. Never stage these, even if `git status` somehow shows them

- Anything under `system/confidentials/` (the repo's `.gitignore` already excludes it — if it shows up
  anyway, that's a sign the ignore rule broke, not a green light to commit it)
- Any `.env` file, credential, API key, or token — even in a file whose name looks unrelated.
  Double-check contents of anything unfamiliar before staging it, not just the filename
- Anything under `system/data/` that's supposed to stay local (drop/, logs/, processed/,
  project_master/, corpus_index.db, pending_update/, pending_settings/) — same gitignore, same
  check

If something suspicious is staged or about to be, **stop and flag it to the user** instead of
committing it. This check happens every run, not just the first time.

## 3. Stage deliberately

Prefer adding specific files by name over `git add -A`. Look at what `git status` actually shows
and stage the files that are genuinely this session's work. If an untracked file's purpose isn't
obvious, ask before including it rather than sweeping it in.

## 4. Write the message like the existing history does

Check `git log --oneline -10` for tone before drafting. This repo's commits are short, describe
the fix/change and its impact or reason, not a changelog of every line touched — match that
rather than defaulting to a generic style. One to two sentences, via heredoc, always ending with:

```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
```

Always a **new** commit — never `--amend`, never `--no-verify`, never bypass signing. If a
pre-commit hook fails, fix the underlying issue and make a new commit; don't skip past it.

## 5. Push

`git push`, or `git push -u origin <branch>` if this branch has no upstream yet.

**If the push is rejected** (remote has diverged, needs a merge or rebase) — stop. Do not force
push, do not rebase or merge without asking first, regardless of how this skill was invoked.
Explain what happened and ask the user how they want to resolve it. Force-push is never
pre-authorized by this skill, on `main` or anywhere else.

## 6. Report back, plainly

After pushing, `git status` once more and state the commit hash, branch, and push result. If
there was nothing to commit, say that — don't fabricate an empty commit to have something to
report.
