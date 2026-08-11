"""
check_no_leaks.py
-----------------
PreToolUse hook. Blocks `git commit` / `git push` when the content about to be
committed contains firm-confidential data or a credential.

WHY THIS EXISTS. This repo is deliberately PUBLIC (a portfolio piece). On
2026-07-29 an audit found the firm's real deal names, prices, addresses and
counterparties in tracked files -- publicly fetchable. Those were genericized,
the business docs untracked, and the real property list moved to a gitignored
JSON. This hook is what stops it happening again: a subagent audit only runs
when someone remembers to run it, but a hook runs on every single commit.

WHY THE PATTERN LIST IS NOT IN THIS FILE. The confidential thing IS the list of
names. Hardcoding them here would publish, in the public repo, exactly what the
hook exists to keep out of it. So the names live in
`.claude/hooks/leak_patterns.txt` (gitignored) and this file just reads them.
The mechanism is public; the secrets are not.

If that file is missing, this BLOCKS commit/push rather than silently allowing
-- a git worktree does not inherit gitignored files from the main checkout, so
an isolated worktree agent starts with no pattern list by default. That used
to fail open with a stderr-only warning, and it is exactly how three real
property names reached tracked history on 2026-08-04/08-06 undetected. Copy
the file into the worktree (or commit from the main working directory) to
unblock.

Exit contract: prints a PreToolUse JSON decision on stdout. Never raises --
a crashing security hook that fails open is worse than no hook, so anything
unexpected falls through to "allow" with a warning on stderr.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATTERNS_FILE = REPO / ".claude" / "hooks" / "leak_patterns.txt"

# Paths that must never be tracked. Safe to name in public: these are the
# FILENAMES of confidential documents, not their contents, and .gitignore
# already lists them in the clear.
FORBIDDEN_PATHS = [
    r"^docs/PORTFOLIO_STANDARD\.md$",
    r"^docs/COMPANY_PROFILE\.md$",
    r"^docs/EVIDENCE_APPENDIX\.md$",
    r"^docs/jurisdictions/",
    r"^docs/agents/.*/memory\.md$",
    r"builtin_properties\.json$",
    r"(^|/)\.env$",
    r"^confidentials/(?!\.env\.template)",
    r"^data/(drop|project_master|processed|logs|pending_)",
    r"corpus_index\.db",
]

# Credential shapes. Generic by nature, so fine to keep in public code.
SECRET_PATTERNS = [
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key block"),
    (r"(?i)\b(api[_-]?key|secret|token|passwd|password)\b\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}",
     "credential assigned to a secret-looking name"),
]


def _decision(allow: bool, reason: str = "") -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def _git(*args: str) -> str:
    """
    Always returns a str, never None -- callers .splitlines() the result.

    encoding/errors are explicit and load-bearing: text=True alone decodes with
    the Windows ANSI codepage (cp1252 here), and these files are full of em
    dashes, so the very first real run raised UnicodeDecodeError and the
    fail-open guard let the commit through. A security hook that silently
    passes everything is the worst outcome available, so decode defensively.
    """
    try:
        r = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=25)
        return r.stdout or "" if r.returncode == 0 else ""
    except Exception as e:
        print(f"check_no_leaks: git {' '.join(args)} failed: {e}", file=sys.stderr)
        return ""


def _load_name_patterns() -> tuple[list[str], bool]:
    """(patterns, list_was_found). Absent list is a warning, not a pass."""
    if not PATTERNS_FILE.exists():
        return [], False
    pats = []
    for line in PATTERNS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append(line)
    return pats, True


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _decision(True)

    cmd = (payload.get("tool_input") or {}).get("command") or ""

    # Match git only in COMMAND POSITION -- start of the string, or straight
    # after a shell separator. A bare \bgit\s+commit\b search also fires on the
    # phrase inside a quoted argument, which really happened: it blocked
    # `echo '...git commit...' | python hook.py` while testing this very file.
    # Over-blocking teaches people to disable the hook, so precision matters.
    GIT_WRITE = r"(?:^|[\n;&|]|&&|\|\|)\s*(?:sudo\s+)?git\s+(?:-[^\s]+\s+)*(commit|push)\b"
    m = re.search(GIT_WRITE, cmd)
    if not m:
        _decision(True)

    is_push = m.group(1) == "push"
    # What's about to enter history: staged content for a commit, or the
    # commits ahead of the tracked remote for a push.
    # --diff-filter=ACMR excludes deletions on purpose. UNTRACKING a
    # confidential file is the fix, not the leak, and without this filter the
    # very commit that removes docs/PORTFOLIO_STANDARD.md gets blocked for
    # containing docs/PORTFOLIO_STANDARD.md. Measured, not theoretical.
    if is_push:
        upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}").strip()
        rng = f"{upstream}...HEAD" if upstream else "HEAD~5...HEAD"
        diff = _git("diff", rng)
        names = _git("diff", "--name-only", "--diff-filter=ACMR", rng)
    else:
        diff = _git("diff", "--cached")
        names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR")

    # THE COMMIT MESSAGE IS PART OF PUBLIC HISTORY TOO, and until 2026-08-11
    # nothing checked it. Measured failure, not hypothetical: three commits
    # that day (cf6ebd2, 8a4cd68, 7c05a4e) named real properties in their
    # message prose while their diffs were clean and correctly genericized --
    # including, with some irony, the message of the commit whose whole
    # purpose was removing those same names from a tracked file. A message is
    # as permanent and as public as a diff, so it gets scanned identically.
    #
    # Extracted BEFORE the empty-diff early-exit below, deliberately: the
    # first version of this fix sat after it and therefore never ran for the
    # case it was written for. A message can leak with an empty staged diff.
    if is_push:
        # No -m to read on a push, so take the messages of the commits
        # actually being pushed. Body included, not just the subject line.
        message = _git("log", "--format=%B", rng)
    else:
        # Every -m/--message value in the command. Covers -m "x",
        # --message=x, and the heredoc form ( -m "$(cat <<'EOF' ... EOF )" )
        # this project uses in practice, whose body is part of `cmd` verbatim
        # by the time the hook sees it.
        message = "\n".join(
            re.findall(r"(?:-m|--message[= ])\s*(.+?)(?=\s+-[a-zA-Z-]|\s*$)",
                       cmd, re.S)
        )

    if not diff and not names and not message.strip():
        _decision(True)

    findings = []

    for path in [p.strip() for p in names.splitlines() if p.strip()]:
        for pat in FORBIDDEN_PATHS:
            if re.search(pat, path):
                findings.append(f"{path} is confidential and must not be tracked")
                break

    # Only added lines matter -- a diff that REMOVES a real name is the fix,
    # not the leak, and scanning the whole hunk would block every redaction.
    added = "\n".join(l[1:] for l in diff.splitlines()
                      if l.startswith("+") and not l.startswith("+++"))

    # The message is scanned on exactly the same footing as added diff lines.
    added += "\n" + message

    for pat, label in SECRET_PATTERNS:
        if re.search(pat, added):
            findings.append(f"looks like a {label}")

    name_pats, have_list = _load_name_patterns()
    if not have_list:
        # FAIL CLOSED, not open. This used to be a stderr-only warning
        # followed by an allow -- and that silently disabled the one check
        # that matters most (the name blocklist) for a real, measured case:
        # a git worktree does NOT inherit gitignored files from the main
        # checkout, so an agent given `isolation: "worktree"` (a normal,
        # supported way to parallelize work) starts with this file simply
        # absent. Three real property names reached tracked history this
        # way on 2026-08-04/08-06 -- the hook ran, found no list, printed a
        # note nobody saw, and allowed the commit. Structural/credential
        # checks below still don't need the list and still ran; only the
        # name check was silently skipped. For a public repo, "I can't
        # check for leaked names" must block, not pass.
        findings.append(
            f"{PATTERNS_FILE.name} not found in this working directory -- "
            f"the confidential-name check cannot run here. If this is a git "
            f"worktree, copy {PATTERNS_FILE} from the main repo working "
            f"directory into this one, or commit from the main working "
            f"directory instead."
        )
    else:
        for pat in name_pats:
            try:
                m = re.search(pat, added, re.IGNORECASE)
            except re.error:
                continue
            if m:
                # Report the pattern that fired, never the matched text --
                # this message is shown in a transcript that may itself be
                # shared.
                findings.append(f"confidential-name pattern matched (/{pat}/)")

    if findings:
        bullets = "\n".join(f"  - {f}" for f in dict.fromkeys(findings))
        _decision(False,
                  f"BLOCKED: this {'push' if is_push else 'commit'} would put "
                  f"confidential data in a PUBLIC repo.\n{bullets}\n"
                  f"Fix the content (genericize the name, untrack the file, or "
                  f"move the value into confidentials/.env) and retry. Real "
                  f"names belong in docs/EVIDENCE_APPENDIX.md, which is "
                  f"gitignored.")

    _decision(True)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail closed on a bug in this file
        print(f"check_no_leaks.py error (allowing): {e}", file=sys.stderr)
        _decision(True)
