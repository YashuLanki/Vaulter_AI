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
- **Publishing now installs on every machine with nobody asked (2026-11-19).** Each teammate's
  Vaulter AI installs the release by itself and Claude Desktop is restarted after the computer has
  been idle 15 minutes. The canary step is therefore the only gate: do not promote until it has
  been seen working.
- **Nothing here is urgent.** If any step looks wrong, stop and ask. An unpublished fix
  costs a day; a bad release costs everyone's trust in automatic updates.

---

## Steps

### 1. Run the checks, and stop if any fail

```bash
python system/scripts/check_screener.py
python system/scripts/check_portfolio_comparison.py
python system/scripts/check_answers.py
python system/scripts/check_auto_update.py
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

### 5. Watch it install itself on a real install (the canary)

**Not on the development copy.** The maintainer's own copy is a git checkout, and a git checkout
never updates itself automatically (it would overwrite uncommitted work). A test there no longer
tests what teammates get. Use a real install that is one version behind:

- **The canary install** is a folder outside the repo, for example `%USERPROFILE%\Vaulter Canary\`,
  made from a handoff package of the PREVIOUS release: `python system/scripts/build_handoff.py`
  run on that release, then `system/data/handoff/Vaulter AI/` copied to the scratch folder and set
  up with its `quick_start` installer. Its `system/confidentials/.env` has
  `VAULTER_UPDATE_CHANNEL=canary`. Keep it between releases: each release brings it up to date,
  which is exactly the test, so it is always one version behind the next one.
- **Point Claude Desktop at it for the test** (Settings, Developer, Edit Config: the vaulter_ai
  entry's path to `main.py`), and back to the usual copy afterwards.

Then watch both automatic paths:

1. **Startup install.** Ask one question in a new conversation (that checks the channel and stages
   the release), then fully quit Claude Desktop and reopen it. Confirm:
   - `system/data/logs/auto_install.txt` in the canary install ends with `applied=True` and the
     version you just published
   - its `system/VERSION` reads the new version, `system/data/pending_update/` is empty, and
     `system/data/startup_installed.json` names the new version
   - `Get-ScheduledTask -TaskName "Vaulter AI - Update restart"` exists and its action names the
     canary install's `auto_restart.py`
   - a new conversation says nothing about updates, and an ordinary request answers normally
   - after 15 idle minutes `system/data/logs/auto_restart.txt` has NO new "restarted" line (a
     version installed at startup owes no restart)
2. **One idle restart, with the owner's OK** (it closes Claude Desktop). Make a restart owed by
   deleting the canary install's `system/data/startup_installed.json`, then run, from the canary
   install's `system` folder and with the same Python that Claude Desktop's config names for it:

   ```bash
   <that python.exe> scripts\auto_restart.py --ignore-idle
   ```

   Confirm Claude Desktop closes and reopens by itself, `auto_restart.txt` gains one line ending
   `reopened: yes`, no new `[ERROR]` appears in `system/data/logs/vaulter.log`, and any Claude Code
   window opened outside Desktop is still running.

   **A release that changes `requirements.txt` is never installed at startup**, only by this
   path. For such a release do the idle restart FIRST, instead of the startup install: once the
   release is staged, run the command above without restarting Claude by hand, and confirm it
   ends `installed; reopened: yes`.

**Afterwards, on the maintainer's machine only:** point Claude Desktop back at the usual copy and
remove the canary's task (`schtasks /Delete /TN "Vaulter AI - Update restart" /F`), otherwise it
keeps restarting Claude Desktop for the canary install.

*Why this and not "it looks fine":* nobody is asked any more, so the canary is the only place a
bad release can be caught before it installs itself everywhere. An apply once ran long enough to
time out and was reported as FAILED while it had SUCCEEDED; only watching a real install do it
tells you which. The installer puts the previous version back by itself if a release cannot even
be imported, but a release that imports and then misbehaves is only caught here.

### 6. Promote

```bash
python system/scripts/release.py --promote
```

Now it reaches everyone on the default channel, and nobody is asked: each machine checks the
channel at least hourly while it is on (a tool call or the restart task), installs the release the
next time Claude Desktop starts, or after the computer has been idle 15 minutes it closes Claude
Desktop, installs and reopens it. A machine that is off or logged out picks it up when next used.

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
- **The canary install does not install the release by itself**, puts the previous version
  back, or Claude Desktop does not reopen. Each of these has a line in the canary install's
  `system/data/logs/` (`auto_install.txt`, `auto_restart.txt`, `vaulter.log`); read it first.
- **You are not on the machine with the signing key.** Nothing you do will work; say so.

## What this does not cover

- **Machines where updates are not automatic.** A Mac, a development copy, or a Windows machine
  whose restart task could not be set up (one `[ERROR]` in the team's error reports says so)
  keeps the old behaviour: Claude asks before installing and asks for a restart.
- **A machine whose restart task gave up.** After two restarts that did not load a version, the
  task stops for that version, sends one `[ERROR]` to the team, and Claude asks the person once
  to quit and reopen Claude. A restart by hand lets the task try again.
- **Long work in progress.** "Idle" means no keyboard or mouse for 15 minutes, so a long job
  Claude is running on its own is ended by the restart.
- **A machine that cannot see the team folder.** It reads its updates from that folder, so
  it will never be offered anything. That one needs the zip from step 7, by hand.

## Reporting back

Say which version went out, that the checks passed, and what the canary showed (startup
install and idle restart). Then say plainly who it reaches and who it does not: machines that
are off or logged out install it when next used, which is the normal case rather than a failure,
and machines where updates are not automatic are still asked.
