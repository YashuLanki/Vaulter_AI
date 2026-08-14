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
import subprocess
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

# The repo now has the same shape as the package it produces (2026-08-03):
# quick_start/ and system/ side by side. So PROJECT_ROOT is system/ -- the
# half that ships -- and REPO_ROOT is its parent, which also holds
# quick_start/ and the dev-only files (docs/, .claude/, CLAUDE.md, .git/)
# that are simply outside the shipped tree and need no exclusion rule now.
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
REPO_ROOT = PROJECT_ROOT.parent
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

def _should_skip(path: Path, rel: Path) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
        return True
    if path.is_file():
        if path.suffix in EXCLUDED_FILE_SUFFIXES or path.name in EXCLUDED_FILE_NAMES:
            return True
    return False


def _detected_library_url() -> str | None:
    """
    The SharePoint web address of the firm's document library, as recorded by
    OneDrive on THIS machine.

    Windows keeps a map of every synced library -- local folder to web address
    -- under HKCU\Software\SyncEngines\Providers\OneDrive. Reading it means
    the package can carry the one link that lets a teammate sync the library
    themselves, instead of being talked through finding it.

    Read at build time and never stored in this repo: the address contains the
    firm's SharePoint tenant, which is real account detail and this repo is
    public. Returns None on anything unexpected -- the package then simply
    carries no link and setup explains in words instead.
    """
    try:
        import winreg
        corpus = _detected_library_name()
        if not corpus:
            return None
        key = r"Software\SyncEngines\Providers\OneDrive"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as root:
            for i in range(winreg.QueryInfoKey(root)[0]):
                try:
                    with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                        mount = str(winreg.QueryValueEx(sub, "MountPoint")[0])
                        if Path(mount).name != corpus:
                            continue
                        url = str(winreg.QueryValueEx(sub, "UrlNamespace")[0]).strip()
                        return url or None
                except OSError:
                    continue
    except Exception:
        pass
    return None


def _detected_library_name() -> str | None:
    """
    The folder name of the firm's document library, as seen on THIS machine.

    Read at build time rather than stored anywhere: the name is real tenant
    detail and this repo is public, so it must never appear in tracked source.
    Returns None if this machine cannot resolve it either, in which case the
    package simply does not name one and the recipient falls back to detection.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        import config
        if config.CORPUS_DIR is None:
            return None
        return Path(config.CORPUS_DIR).name
    except Exception:
        return None


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


def _copy_claude_tooling(package_root: Path) -> int:
    """
    The QA subagents and skills, plus CLAUDE.md.

    Honest caveat, so nobody expects more than this delivers: **Claude Desktop
    does not load a project's subagents or skills from disk -- only Claude Code
    does.** A teammate using Desktop + the MCP server gets the 21 tools and
    nothing from here. These are shipped anyway because they cost a few
    kilobytes, they're already public in the repo, and they make the package
    complete if anyone ever opens it in Claude Code.

    Deliberately partial: `.claude/hooks/` is NOT copied. It contains
    leak_patterns.txt -- the literal list of confidential names the leak hook
    exists to catch -- which is the single worst file in the project to ship.
    `settings*.json` is skipped too (per-machine paths and personal config).
    """
    copied = 0
    for sub in ("agents", "skills"):
        src_dir = REPO_ROOT / ".claude" / sub
        if not src_dir.is_dir():
            continue
        for src in src_dir.rglob("*"):
            if not src.is_file() or src.suffix.lower() != ".md":
                continue
            target = package_root / ".claude" / sub / src.relative_to(src_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            copied += 1
    guide = REPO_ROOT / "CLAUDE.md"
    if guide.exists():
        # Inside system/, not at the top level. To the teammate this package
        # is FOR, CLAUDE.md is a developer document sitting in their way at
        # exactly the moment they are deciding what to click -- and the top
        # level should hold one obvious instruction and nothing else.
        #
        # Marking it hidden instead was tried first and does NOT work: the zip
        # format doesn't carry Windows file attributes, so it arrived hidden
        # in the built folder and plainly visible again the moment anyone
        # unzipped it. Verified by extracting the real zip and re-reading the
        # attributes. Location survives; attributes do not.
        #
        # Cost: someone opening this package in Claude Code won't get CLAUDE.md
        # loaded automatically from the root. Accepted -- that is the rare
        # case this package explicitly does not optimise for, and the file is
        # still right there in system/.
        (package_root / "system").mkdir(parents=True, exist_ok=True)
        shutil.copy2(guide, package_root / "system" / "CLAUDE.md")
        copied += 1
    return copied


# The only settings a packaged .env may contain. Anything else means a real
# .env has been picked up by mistake, and the package must be refused.
_PACKAGEABLE_ENV_KEYS = {"VAULTER_CORPUS_SUBFOLDER", "VAULTER_CORPUS_HINT",
                          "VAULTER_LIBRARY_URL"}


def _env_is_packageable(path: Path) -> bool:
    """
    True only for the tiny .env this script writes itself.

    A .env normally must never travel -- that is why the name is on the
    forbidden list. Since 2026-08-13 the package deliberately carries one, so
    the recipient does not have to be told which SharePoint library to use.
    That exception is checked by CONTENT, not by trusting that we wrote it:
    every setting must be one of a short allowlist, so a real .env that got
    copied in by accident still fails the check and stops the build. Fails
    closed -- an unreadable file is refused, never waved through.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key not in _PACKAGEABLE_ENV_KEYS:
            return False
    return True


def _verify_no_secrets(package_root: Path) -> list:
    """Walk the real built output and report anything that must not ship."""
    offenders = []
    for path in package_root.rglob("*"):
        if not path.is_file() or path.name not in FORBIDDEN_IN_PACKAGE:
            continue
        if path.name == ".env" and _env_is_packageable(path):
            continue  # the library-name file this script writes; contents checked
        offenders.append(str(path.relative_to(package_root)))
    return offenders


def main() -> int:
    print("Vaulter AI -- building the handoff folder")
    print(f"Source: {PROJECT_ROOT}\n")

    # Default ON, because the whole point of the package is that the recipient
    # does nothing. Pass --no-library to build one that names no library --
    # appropriate if the zip is going somewhere the firm's folder name should
    # not travel. This is deliberately loud in the output either way, so it is
    # never a silent property of the package.
    include_library = "--no-library" not in sys.argv

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

    # 2b. Tell the package which document library to use, so the recipient never
    #     has to work it out. Read from THIS machine at build time -- never
    #     hardcoded, so the name stays out of this public repo, and never
    #     committed, since data/ is excluded from git.
    #
    #     Why this is worth doing: detection is good but not omniscient. An
    #     organisation has several SharePoint libraries, and a teammate syncing
    #     the site's default "Documents" one but not the firm's document library
    #     leaves nothing to detect -- measured on two real machines, 2026-08-13.
    #     Naming it outright removes the guesswork and the round trip.
    #
    #     Setup only creates .env when it is absent, so this survives install.
    #     Both settings are written: the exact folder name wins outright, and
    #     the distinctive word is a fallback in case a colleague's copy is named
    #     slightly differently (confirmed 2026-07-29 that capitalisation varies).
    if include_library:
        library = _detected_library_name()
        if library:
            hint = library.split(" - ")[-1].strip() or library
            lines = [
                "# Written by build_handoff.py from the machine that built this",
                "# package, so setup does not have to guess which SharePoint",
                "# library holds the firm's documents.",
                f"VAULTER_CORPUS_SUBFOLDER={library}",
                f"VAULTER_CORPUS_HINT={hint}",
            ]
            url = _detected_library_url()
            if url:
                lines += [
                    "# Where that library lives on SharePoint, so setup can open it",
                    "# for someone who has not synced it to their computer yet.",
                    f"VAULTER_LIBRARY_URL={url}",
                ]
            (dest_system / "confidentials" / ".env").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
            print("  system/confidentials/.env  (document library pre-set for the recipient"
                  + (", with its SharePoint link" if url else "") + ")")
        else:
            print("  ! Could not read this machine's document library, so the package "
                  "does not name one.")
            print("    The recipient's setup will fall back to detecting it.")
    else:
        print("  system/confidentials/.env  SKIPPED (--no-library) -- the recipient's "
              "setup will detect the library itself.")

    # 3. The only folder the recipient ever opens. Windows-only for now: the
    # team is on Windows, and a second file (the Mac .command launcher) just
    # reads as "which one do I click?" to someone non-technical. The .command
    # launcher stays in the repo -- add it back into a package deliberately
    # if a teammate ever needs it, rather than shipping it to everyone by
    # default. This used to skip a "how to start" file too, reasoning that
    # the wizard's own printed output already walks through every step. That
    # reasoning had a hole, found 2026-08-12 by watching a real first run:
    # someone opened the zip WITHOUT extracting it, browsed into quick_start,
    # and double-clicked Setup there. Windows can't run a program from inside
    # a zip, so it interrupted with its own extract-first dialog -- and the
    # wizard, whose output was supposed to be the guidance, had not run and
    # could not run. Guidance that only exists once the program starts cannot
    # help someone who is stuck before it starts.
    #
    # It goes at the PACKAGE ROOT, not in quick_start beside the launcher --
    # that was the first attempt and it was still one step too late. Opening
    # the zip shows one folder; opening that folder had nothing to read at
    # all; and the guidance only appeared in quick_start, arriving at the
    # same moment as the Setup file itself. By then the tempting thing to
    # click is already on screen. At the root it is read BEFORE there is
    # anything to click wrongly -- and it is the only visible file there,
    # since CLAUDE.md is hidden (see _copy_claude_tooling).
    START_GUIDE = "How to start.txt"
    for launcher in sorted((REPO_ROOT / "quick_start").iterdir()):
        if not launcher.is_file() or launcher.suffix == ".command":
            continue
        if launcher.name == START_GUIDE:
            shutil.copy2(launcher, package_root / START_GUIDE)
            print(f"  {START_GUIDE}")
        else:
            shutil.copy2(launcher, dest_quick / launcher.name)
            print(f"  quick_start/  {launcher.name}")

    # 3b. QA subagents, skills and the project guide -- see the docstring above
    #     for why this is partial and what it does NOT do for a Desktop user.
    n = _copy_claude_tooling(package_root)
    print(f"  .claude/      {n} subagent/skill files (CLAUDE.md tucked into system/)")

    # 3b. Stamp the version, exactly as release.py does for update packages.
    #
    # Without this the install has no .git and no VERSION, so
    # _get_code_version() returns "unknown" -- and since
    # _check_and_stage_update only compares remote != current, "unknown"
    # differs from every real version. A brand-new teammate would be
    # prompted to apply an update on their first day, before they had
    # done anything. Stamping the commit the package was built from makes
    # that comparison meaningful from the start.
    version = "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            version = result.stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        # Narrow on purpose. A bare `except Exception` here already hid a real
        # bug once -- subprocess wasn't imported, the NameError was swallowed,
        # and every package silently shipped VERSION="unknown" while looking
        # like it had worked.
        print(f"      (could not read the git version: {e})")
    # Second line: the commit's own date. This is what lets a fresh install
    # tell a genuinely NEWER published release from a merely different one --
    # without it, a zip built after the last publish gets offered a DOWNGRADE
    # the first time it checks (measured 2026-08-12 on a real fresh install).
    commit_time = ""
    try:
        r = subprocess.run(["git", "show", "-s", "--format=%cI", "HEAD"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10)
        if r.returncode == 0:
            commit_time = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    stamp = (version + "\n" + commit_time + "\n") if commit_time else version
    (dest_system / "VERSION").write_text(stamp, encoding="utf-8")
    print(f"  system/VERSION  {version}")
    if version == "unknown":
        print("      ⚠ no version stamp — a fresh install will be prompted to "
              "apply an update on first use")

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
    print("\nSend the .zip. Opening it shows one instruction file, \"How to start\", "
          "plus quick_start (the only folder they open) and system (the program). "
          "CLAUDE.md now sits inside system/, and .claude/ only matters if someone "
          "opens this in Claude Code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
