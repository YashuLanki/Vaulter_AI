"""
apply_update.py
----------------
Vaulter AI — apply a staged update (Priority 4 in
docs/MULTI_USER_TRANSITION.md).

The primary, non-technical way to use this is to just ask Claude in a
normal conversation once check_system_health mentions an update is
ready ("go ahead and install it") -- the apply_pending_update MCP tool
(see mcp_server.py) calls straight into apply_pending_update() below,
no terminal needed. This file's own command-line entry point
(`python apply_update.py`) is kept as a manual/troubleshooting fallback,
not the expected everyday path.

This is deliberately a human-confirmed step, not something that runs on
its own -- nothing in this project downloads AND applies an update
without that confirmation; the scheduler only ever stages one (see
mcp_server.py's _check_and_stage_update). Applying means:
  1. Extracting the staged code package to a temp folder.
  2. Deleting any file in this project that the new version no longer
     has (so removed files don't linger as stale dead code).
  3. Copying every file from the new version into place.
  4. Re-installing from requirements.txt with the same Python already
     running this project, in case the update added/changed a
     dependency -- otherwise the new code could reference a package
     that was never installed.
  5. Cleaning up the staging area.

confidentials/ and data/ are NEVER touched -- the update package itself
never contains them (see release.py's exclusion list), so this can't
and doesn't delete or overwrite your secrets or local data. The virtual
environment's FILES aren't touched either, though step 4 does install
packages into it, same as any other `pip install`.

You'll need to restart Claude Desktop (and the MCP server it launches)
afterward for the new code to actually take effect -- nothing here does
that for you, since Claude Desktop can't be restarted from inside its
own MCP server process.
"""

import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# Must match release.py's EXCLUDED_DIR_NAMES exactly -- these are never
# in the update package to begin with, so they must never be treated as
# "stale files to delete" just because they're not in the new version.
PRESERVED_DIR_NAMES = {
    ".git", "venv", ".venv", "env", "ENV", "confidentials", "data",
    "__pycache__", ".pytest_cache", ".mypy_cache",
}


def _is_preserved(rel_path: Path) -> bool:
    return any(part in PRESERVED_DIR_NAMES for part in rel_path.parts)


def _load_pending() -> dict | None:
    from config import PENDING_UPDATE_DIR
    ready_path = PENDING_UPDATE_DIR / "ready.json"
    if not ready_path.exists():
        return None
    return json.loads(ready_path.read_text())


def apply_update(project_root: Path, zip_path: Path) -> tuple[int, int]:
    """
    Extracts zip_path and syncs project_root to match it exactly, except
    for anything under a PRESERVED_DIR_NAMES path. Returns (files_added_
    or_updated, files_deleted). Pulled out as its own function, taking
    project_root explicitly, specifically so tests can point it at a
    scratch directory instead of the real project.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_dir)

        new_files = {
            p.relative_to(tmp_dir) for p in tmp_dir.rglob("*") if p.is_file()
        }

        # Step 1: delete anything in the project that the new version no
        # longer has (and isn't a preserved path) -- otherwise a file
        # removed upstream would linger here forever as stale dead code.
        deleted = 0
        for existing in list(project_root.rglob("*")):
            if not existing.is_file():
                continue
            rel = existing.relative_to(project_root)
            if _is_preserved(rel):
                continue
            if rel not in new_files:
                existing.unlink()
                deleted += 1

        # Step 2: copy every file from the new version into place.
        updated = 0
        for rel in new_files:
            src = tmp_dir / rel
            dest = project_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            updated += 1

        # Remove any now-empty directories left behind by step 1 (never
        # inside a preserved path, and never the project root itself).
        for d in sorted(project_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if d.is_dir() and not _is_preserved(d.relative_to(project_root)) and not any(d.iterdir()):
                d.rmdir()

        return updated, deleted


# The only two places a launcher/agent-file package may write, relative to the
# install root. Anything else in such a package is ignored rather than trusted.
ALLOWED_EXTRA_TOP_LEVEL = ("quick_start", ".claude")

# Never written even when present in a package: the hooks folder holds the
# confidential-name list the leak hook uses, and settings files are per-machine.
# release.py already excludes both; this is the receiving end refusing them
# independently, so a package built by an older or hand-made script cannot
# place them either.
REFUSED_EXTRA_PATHS = (Path(".claude") / "hooks",)
REFUSED_EXTRA_NAMES = {"settings.json", "settings.local.json", "leak_patterns.txt"}


def apply_extras(install_root: Path, zip_path: Path) -> tuple[int, int]:
    """
    Copy the launcher (`quick_start/`) and agent/skill files (`.claude/`) into
    place. Returns (files written, entries refused).

    Two deliberate differences from how the program itself is updated:

    * **Nothing is ever deleted here.** The program folder is synced to match
      the package exactly, so a file removed upstream does not linger as dead
      code. These are instructions, not running code, and this folder is also
      where somebody may reasonably have put an agent of their own -- deleting
      "anything not in the package" would throw that away to solve a problem
      (a stale instruction file) that costs nothing.
    * **Every entry is checked against an allowlist before it is written**,
      because this is the first thing in the update path that writes OUTSIDE
      the program folder. A package naming `../confidentials/.env` or an
      absolute path would otherwise land wherever it liked. Both the folder
      and the resolved destination are checked, so a crafted path cannot
      escape the install root.
    """
    written = refused = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = Path(info.filename)
            dest = (install_root / rel).resolve()

            ok = (
                not rel.is_absolute()
                and ".." not in rel.parts
                and rel.parts[0] in ALLOWED_EXTRA_TOP_LEVEL
                and not any(rel.is_relative_to(bad) for bad in REFUSED_EXTRA_PATHS)
                and rel.name not in REFUSED_EXTRA_NAMES
                # From .claude/, only instruction files. Mirrors release.py's
                # own rule so a package built elsewhere, or by an older script,
                # still cannot drop anything else into that folder.
                and not (rel.parts[0] == ".claude" and rel.suffix.lower() != ".md")
                and dest.is_relative_to(install_root.resolve())
            )
            if not ok:
                refused += 1
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written += 1
    return written, refused


def refresh_dependencies(project_root: Path) -> tuple[bool, str]:
    """
    Re-installs from requirements.txt using the SAME Python interpreter
    already running this project, so a fix that adds or changes a
    dependency doesn't leave the app broken after its code is updated
    but the new package it needs isn't installed. pip skips
    already-satisfied packages quickly, so this is safe and fast to run
    on every apply, not just when requirements.txt actually changed.

    Returns (ok, message) -- message is empty on success, or pip's own
    error output (truncated) on failure. Never raises.
    """
    requirements = project_root / "requirements.txt"
    if not requirements.exists():
        return True, ""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-2000:]
    return True, ""


def apply_pending_update(project_root: Path = None) -> dict:
    """
    Applies whatever update is currently staged, with NO interactive
    prompt -- callers (this file's own main(), or the
    apply_pending_update MCP tool in mcp_server.py) are responsible for
    getting a human's explicit go-ahead BEFORE calling this; this
    function just does the work once called. Never raises for the
    ordinary "nothing staged" / "staged file missing" cases -- always
    returns a result dict describing what happened.
    """
    if project_root is None:
        project_root = PROJECT_ROOT

    pending = _load_pending()
    if not pending:
        return {"applied": False, "reason": "no update is currently staged"}

    from config import PENDING_UPDATE_DIR

    version = pending.get("version")
    zip_path = PENDING_UPDATE_DIR / pending["zip_filename"]
    if not zip_path.exists():
        return {
            "applied": False,
            "reason": f"the staged update record points to a missing file ({zip_path.name}) "
                      f"-- it will be re-downloaded on the next check",
        }

    # Re-verify here too, not just at staging time -- belt and suspenders.
    # _check_and_stage_update() already checked the signature before writing
    # ready.json, but this is the step that actually overwrites project
    # files, so it re-checks against the signature stored in ready.json
    # rather than trusting that nothing touched the staged zip in between.
    from core.release_signing import verify_bytes, PUBLIC_KEY_PATH

    signature_b64 = pending.get("signature")
    verified = False
    if signature_b64 and PUBLIC_KEY_PATH.exists():
        try:
            digest = hashlib.sha256(zip_path.read_bytes()).digest()
            verified = verify_bytes(digest, base64.b64decode(signature_b64))
        except Exception:
            verified = False
    if not verified:
        return {
            "applied": False,
            "reason": f"the staged package for version {version} failed signature "
                      f"verification at apply time -- refusing to apply it. Delete "
                      f"{zip_path} and the pending update record, then let it re-download.",
        }

    updated, deleted = apply_update(project_root, zip_path)

    # The launcher and agent files, if this release staged them. Verified again
    # here, right before writing, exactly as the program package is -- and any
    # failure is reported without touching the program update that just
    # succeeded. Never raises: a broken installer package must not turn a
    # working code update into an apparent crash.
    extras_written = 0
    extras_note = ""
    extras_name = pending.get("extras_zip_filename")
    extras_sig = pending.get("extras_signature")
    if extras_name and extras_sig:
        extras_path = PENDING_UPDATE_DIR / extras_name
        try:
            if not extras_path.exists():
                extras_note = "the launcher and agent files were not downloaded this time"
            else:
                digest = hashlib.sha256(extras_path.read_bytes()).digest()
                if verify_bytes(digest, base64.b64decode(extras_sig)):
                    extras_written, refused = apply_extras(project_root.parent, extras_path)
                    if refused:
                        extras_note = (f"{refused} item(s) in the launcher package were "
                                       f"refused for being outside the two folders it is "
                                       f"allowed to write")
                else:
                    extras_note = ("the launcher and agent files failed their signature "
                                   "check and were not applied -- the program update was")
        except Exception as e:
            extras_note = f"the launcher and agent files could not be applied ({e})"
        extras_path.unlink(missing_ok=True)

    deps_ok, deps_message = refresh_dependencies(project_root)

    zip_path.unlink(missing_ok=True)
    (PENDING_UPDATE_DIR / "ready.json").unlink(missing_ok=True)

    return {
        "applied": True,
        "version": version,
        "files_updated": updated,
        "files_deleted": deleted,
        "extras_written": extras_written,
        "extras_note": extras_note,
        "dependencies_ok": deps_ok,
        "dependencies_message": deps_message,
    }


def main() -> None:
    """Manual/troubleshooting CLI fallback -- the everyday path is asking
    Claude to apply it, via the apply_pending_update MCP tool, once
    check_system_health mentions one is ready. See this file's own
    module docstring."""
    pending = _load_pending()
    if not pending:
        print("No update is currently staged. check_system_health will mention it here "
              "once one is ready.")
        return

    version = pending.get("version")
    notes = pending.get("notes", "")
    print(f"A staged update is ready: version {version}" + (f" — {notes}" if notes else ""))
    print(f"This will update the code in {PROJECT_ROOT} and refresh its Python dependencies.")
    if pending.get("extras_zip_filename"):
        print("It also refreshes the launcher and the agent/skill files beside it.")
    print("Your confidentials/ and data/ are never touched.")
    answer = input("Apply it now? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Not applying. Run this again whenever you're ready.")
        return

    result = apply_pending_update()
    if not result["applied"]:
        print(f"Could not apply: {result['reason']}")
        return

    print(f"Applied version {result['version']}: {result['files_updated']} file(s) updated, "
          f"{result['files_deleted']} removed.")
    if result.get("extras_written"):
        print(f"Also refreshed {result['extras_written']} launcher and agent file(s).")
    if result.get("extras_note"):
        print(f"Note: {result['extras_note']}.")
    if not result["dependencies_ok"]:
        print(f"WARNING: refreshing dependencies had a problem: {result['dependencies_message']}")

    print()
    print("Done. Fully quit and reopen Claude Desktop for the new code to take effect.")


if __name__ == "__main__":
    main()
