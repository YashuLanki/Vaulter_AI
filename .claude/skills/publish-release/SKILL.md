---
name: publish-release
description: Use to publish a Vaulter AI update so it reaches every teammate's machine — the full sequence from committed code to a promoted release and a rebuilt handoff zip. Invoke when the user asks to publish, release, ship, or push out a fix, or to rebuild the zip for a new person. Refuses to publish when the checks fail.
---

# Publishing an update — orchestrator

This is the most dangerous thing Vaulter AI does: a published release reaches every
teammate's computer, and a bad one reaches all of them at once. It is also the only
operation nobody but the maintainer has ever performed, which is why this file exists.

**Two readers.** The numbered steps are written so a non-technical person can follow them
with Claude doing the work. The reasoning under each one is for whoever needs to know why a
step is there before changing it.

---

## Before you start

- **Only publish code that is committed.** The version number comes from the commit, so
  uncommitted work would ship under a version that does not describe it.
- **Only one machine can publish.** The signing key lives on the maintainer's computer and
  nowhere else. If you are not on that machine, the publish will fail at step 3 and that is
  correct behaviour, not a bug to work around.
- **Nothing here is urgent.** If any step looks wrong, stop and ask. An unpublished fix
  costs a day; a bad release costs everyone's trust in the update prompt.

---

## Steps

### 1. Run the checks, and stop if any fail

```bash
python system/scripts/check_screener.py
python system/scripts/check_portfolio_comparison.py
python system/scripts/check_answers.py
```

**Every one must pass before anything is published.** If a check fails, do not publish —
find out why first. Read the check before assuming the code is wrong: this project has had
checks that encoded a bug, and a failing check is evidence about the check at least as much
as about the code.

*Why this is here:* `release.py` runs **no checks at all**. That judgement has always lived
in whoever was publishing. Putting it in this file is what makes the sequence repeatable by
someone who does not already hold it in their head.

### 2. Commit, and confirm the tree is clean

```bash
git status --short      # must be empty
git log --oneline -1    # this is the version that will ship
```

### 3. Publish to the canary channel

```bash
python system/scripts/release.py
```

This publishes to `canary` only — a channel **nobody is on**. Nothing has reached anyone yet.

*Why two channels:* a bad release caught here costs one machine. Caught after promotion, it
costs everyone.

### 4. Check the package before trusting it

Confirm, for both the program package and the launcher package:

- the signature verifies against `system/release_public_key.pem`
- the version inside matches what you just committed
- nothing confidential is inside — no `confidentials/`, no `docs/`, no real-name list, no
  signing key

*Why:* the package is built by walking the filesystem, not by asking git what is tracked. A
new folder in the wrong place would travel.

### 5. Apply it on a real machine, through the real path

Point the live install at `canary`, let it download and apply the update the way a teammate's
machine would, then put the channel back. Confirm:

- it downloaded and staged the version you just published
- the apply finished **inside a single tool call** — no timeout
- the version file now reads the new version and the staging folder is empty
- that machine's own settings survived
- an ordinary request still answers normally afterwards

*Why this and not "it looks fine":* an apply once ran long enough to time out, and Claude
reported **FAILED** on an update that had actually **SUCCEEDED** — sending someone to fix a
machine that was already working. The only way to know is to watch a real one do it.

### 6. Promote

```bash
python system/scripts/release.py --promote
```

Now it reaches everyone on the default channel, offered inside their own conversation.

### 7. Rebuild the handoff zip

```bash
python system/scripts/build_handoff.py
```

Then confirm the zip carries the version you just published, contains the fixes you expect,
and holds nothing confidential.

*Why it is separate:* a published release only reaches machines that already have the system
and can see the team folder. **A brand-new person, or a machine cut off from the team folder,
needs the zip.** Forgetting this is how a new person installs a version that is already old.

---

## When to stop and ask

- **Any check fails.** Never publish past a red check.
- **A signature does not verify.** Something is wrong with the package; do not promote.
- **The apply does not finish inside one call.** This has happened before and the cause was
  real.
- **You are not on the machine with the signing key.** Nothing you do will work; say so.

## What this does not cover

- **Restarting Claude Desktop.** Nothing can do that for the person — an update cannot
  restart the application running it. The system now says so by itself, on every answer,
  until the restart happens.
- **A machine that cannot see the team folder.** It reads its updates from that folder, so
  it will never be offered anything. That one needs the zip from step 7, by hand.

## Reporting back

Say which version went out, that the checks passed, and what the apply measured. Then say
plainly who it reaches and who it does not — anyone who has not opened a conversation since
will not see it yet, and that is the normal case rather than a failure.
