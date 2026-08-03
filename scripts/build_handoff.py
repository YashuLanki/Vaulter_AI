"""
build_handoff.py
----------------
Vaulter AI — build the send-ready folder for a new teammate.

    python scripts/build_handoff.py

Produces `data/handoff/Vaulter AI/` plus a matching .zip, laid out so a
non-technical person opening it sees exactly two folders and knows
immediately which one to open:

    Vaulter AI/
      quick_start/     <- "Setup Vaulter AI" lives here. This is the only
                          folder they ever need to open.
      system/          <- everything the program itself runs on.

This is deliberately NOT a reorganization of the repo itself. Measured
2026-08-03: moving the code into a subfolder would invalidate ~251 path
references across ~40 markdown files (CLAUDE.md, every agent and skill
doc), for a purely cosmetic gain. Building the handoff layout at package
time gets a cleaner result with none of that risk -- and lets the package
*omit* things a teammate should never receive at all, which a repo
reorganization could not do.

What's deliberately left out, and why:
  * `docs/`      -- internal engineering notes, and the home of the
                    gitignored confidential business documents
                    (PORTFOLIO_STANDARD, COMPANY_PROFILE, jurisdiction
                    dossiers, per-agent memory). Never ships.
  * `.claude/`   -- Claude Code agents/skills/hooks. Developer tooling.
  * `.git/`      -- the whole repo history.
  * `confidentials/` -- except `.env.template`, which the setup wizard
                    needs in order to create a working (blank) `.env`.
                    The real `.env` must never travel.
  * `data/`      -- local runtime data (logs, indexes, CoStar drops, and
                    the gitignored real property list).
  * `CLAUDE.md`, `HISTORY.md`, `.gitignore`, editor/venv/cache folders.

Same spirit as `scripts/release.py`'s own exclusion list -- and the same
reason for it: what's convenient for a developer is clutter, or a leak,
for everyone else.
"""

import os
import shutil
import sys
import zipfile
from pathlib import Path

# Same guard, same reason as setup_wizard.py's: a Windows console defaults to
# a legacy codepage (cp1252) that cannot encode the ✓/✗ this script prints,
# and without this it dies with UnicodeEncodeError on its own status line --
# which is exactly what happened the first time this was run.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
OUTPUT_ROOT = PROJECT_ROOT / "data" / "handoff"
PACKAGE_NAME = "Vaulter AI"

# Top-level names that never go into the package. A denylist rather than an
# allowlist on purpose: a new code folder added later gets shipped
# automatically instead of silently omitted, which would break the install
# for a user with no clue why. The confidentiality risk that trades away is
# covered by the explicit check in `_verify_no_secrets()` below.
EXCLUDED_TOP_LEVEL = {
    ".git", ".github", ".claude", ".idea", ".vscode",
    ".venv", "venv", "env", "ENV",
    "__pycache__", ".pytest_cache", ".mypy_cache",
    "data", "docs", "confidentials", "quick_start",
    ".gitignore", "CLAUDE.md", "HISTORY.md", "VERSION",
}
EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_FILE_NAMES = {".DS_Store", "Thumbs.db"}

# Filenames that must never appear anywhere in a built package, whatever
# path they turn up under. Checked after the copy, against the real output
# tree, rather than trusted to the exclusion rules above -- this is the
# backstop for "someone reorganized something and the denylist went stale."
FORBIDDEN_IN_PACKAGE = {
    ".env", "outlook_token.json", "builtin_properties.json",
    "corpus_index.db", "leak_patterns.txt",
    "PORTFOLIO_STANDARD.md", "COMPANY_PROFILE.md", "EVIDENCE_APPENDIX.md",
}

START_HERE_TEXT = """\
Vaulter AI — start here
=======================

1. Double-click "Setup Vaulter AI" in this folder.

   A black window will open and print what it's doing. That's normal --
   it's the setup running, not an error. Leave it open.

2. Wait. It installs what it needs and connects to Claude Desktop.
   This takes a few minutes. There's nothing to type.

3. When it finishes, fully quit Claude Desktop and open it again.
   (Right-click the Claude icon near the clock, choose Quit.)

4. Start a new conversation and ask it something real, like
   "What properties are in the portfolio?"

If anything looks wrong, or a step reports a warning, stop and ask --
nothing you've done needs undoing to get help.

You never need to open the "system" folder. That's the program itself.
"""


def _should_skip(path: Path, rel: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
        return True
    if path.is_file():
        if path.suffix in EXCLUDED_FILE_SUFFIXES or path.name in EXCLUDED_FILE_NAMES:
            return True
    return False


def _copy_system_files(dest_system: Path) -> int:
    """Everything the program runs on, minus the exclusions. Returns file count."""
    copied = 0
    for entry in sorted(PROJECT_ROOT.iterdir()):
        if entry.name in EXCLUDED_TOP_LEVEL:
            continue
        if entry.is_dir():
            for src in entry.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(PROJECT_ROOT)
                if _should_skip(src, rel):
                    continue
                target = dest_system / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, target)
                copied += 1
        else:
            shutil.copy2(entry, dest_system / entry.name)
            copied += 1
    return copied


def _verify_no_secrets(package_root: Path) -> list:
    """Walk the real built output and report anything that must not ship."""
    offenders = []
    for path in package_root.rglob("*"):
        if path.is_file() and path.name in FORBIDDEN_IN_PACKAGE:
            offenders.append(str(path.relative_to(package_root)))
    return offenders


def main() -> int:
    print("Vaulter AI -- building the handoff folder")
    print(f"Source: {PROJECT_ROOT}\n")

    package_root = OUTPUT_ROOT / PACKAGE_NAME
    if package_root.exists():
        shutil.rmtree(package_root)
    dest_system = package_root / "system"
    dest_quick = package_root / "quick_start"
    dest_system.mkdir(parents=True)
    dest_quick.mkdir(parents=True)

    # 1. The program itself.
    count = _copy_system_files(dest_system)
    print(f"  system/       {count} files")

    # 2. The one file from confidentials/ a fresh install genuinely needs --
    #    without it the wizard's "create your .env" step fails outright.
    template = PROJECT_ROOT / "confidentials" / ".env.template"
    if not template.exists():
        print("  ✗ confidentials/.env.template is missing -- the setup wizard "
              "needs it to create a working .env on the new machine.")
        return 1
    (dest_system / "confidentials").mkdir(parents=True, exist_ok=True)
    shutil.copy2(template, dest_system / "confidentials" / ".env.template")
    print("  system/confidentials/.env.template")

    # 3. The only folder the recipient ever opens.
    for launcher in sorted((PROJECT_ROOT / "quick_start").iterdir()):
        if launcher.is_file():
            shutil.copy2(launcher, dest_quick / launcher.name)
            print(f"  quick_start/  {launcher.name}")
    (dest_quick / "Start Here.txt").write_text(START_HERE_TEXT, encoding="utf-8")
    print("  quick_start/  Start Here.txt")

    # 4. Prove it, don't assume it.
    offenders = _verify_no_secrets(package_root)
    if offenders:
        print("\n  ✗ REFUSING TO PACKAGE -- these must never be sent:")
        for o in offenders:
            print(f"      {o}")
        shutil.rmtree(package_root)
        return 1
    print("  ✓ checked: no secrets, local data, or confidential documents included")

    # 5. The zip is what actually gets sent.
    zip_path = OUTPUT_ROOT / f"{PACKAGE_NAME}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in sorted(package_root.rglob("*")):
            if src.is_file():
                zf.write(src, Path(PACKAGE_NAME) / src.relative_to(package_root))

    size_mb = zip_path.stat().st_size / 1e6
    print(f"\nFolder: {package_root}")
    print(f"Zip:    {zip_path}  ({size_mb:.1f} MB)")
    print("\nSend the .zip. Opening it shows two folders -- quick_start (the one "
          "they open) and system (the program).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
