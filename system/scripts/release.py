"""
release.py
-----------
Vaulter AI — publish a new version for auto-update (Priority 4 in
docs/MULTI_USER_TRANSITION.md).

Run this yourself after merging a reviewed fix to main -- it is NOT run
by staff, and nothing here needs to be automated or triggered by CI;
it's a deliberate, manual "I'm ready to ship this" action.

Usage:
    python release.py                       # publish to the canary channel
    python release.py --notes "fixed X bug"
    python release.py --promote              # promote the current canary
                                              # release to the general channel

Two-step rollout, matching Priority 4's staged-rollout safeguard:
  1. `python release.py` packages the current code and publishes it to the
     CANARY channel only. Only instances with VAULTER_UPDATE_CHANNEL=canary
     in their confidentials/.env (a small number of designated machines --
     see config.py) will pick this up.
  2. Once you've confirmed canary machines are healthy on the new version
     (e.g. via check_system_health), run `python release.py --promote` to
     make that SAME already-published version available to every instance
     on the default "general" channel.

This publishes the version marker and code package to the shared OneDrive
folder (config.UPDATES_DIR) -- the same location every instance's
scheduler already reads from, per Priority 4's design. Each instance only
DOWNLOADS and STAGES a new version automatically; a human still decides
when to actually apply it (see apply_update.py) -- this first version of
the mechanism is deliberately notify-and-stage, not fully automatic.
"""

import argparse
import base64
import hashlib
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# The install root -- one level above the program, holding quick_start/ and
# .claude/. Deliberately NOT folded into PROJECT_ROOT: walking from here would
# put docs/ and confidentials/ inside the tree being packaged, which is the
# confidentiality hole the system/ split closed in the first place. These two
# folders are named explicitly instead, and nothing else up here ever ships.
REPO_ROOT = PROJECT_ROOT.parent

PRIVATE_KEY_PATH = PROJECT_ROOT / "confidentials" / "release_signing_key.pem"

# The two folders that live beside the program and, until 2026-08-19, could
# never reach an already-installed teammate at all -- only a fresh zip. They
# travel in their OWN signed package (see _build_extras_package) rather than
# in the main one, because an install running older code would copy anything
# added to the main package into the program folder, where neither belongs.
# A separate file is simply ignored by code that doesn't know to look for it.
EXTRA_TOP_LEVEL = ("quick_start", ".claude")

# `.claude/hooks/` must NEVER ship: it holds leak_patterns.txt, the literal
# list of confidential names the leak hook exists to catch -- the single worst
# file in the project to publish. settings*.json is per-machine config. Same
# two exclusions build_handoff.py already makes, for the same reasons.
EXCLUDED_EXTRA_PATHS = (
    Path(".claude") / "hooks",
)
EXCLUDED_EXTRA_FILE_NAMES = ("settings.json", "settings.local.json")

# Checked against the built extras package by name, whatever path it appears
# under -- the backstop for "someone moved something and the rules above went
# stale", exactly as build_handoff.py's own FORBIDDEN_IN_PACKAGE does.
FORBIDDEN_IN_EXTRAS = {
    "leak_patterns.txt", ".env", "outlook_token.json",
    "PORTFOLIO_STANDARD.md", "COMPANY_PROFILE.md", "EVIDENCE_APPENDIX.md",
    "release_signing_key.pem",
}

# Never include these in a published package -- secrets, local data,
# virtual environments, and git/OS metadata are all machine-specific or
# sensitive and must never be shipped to a shared location, let alone
# unpacked onto someone else's machine.
EXCLUDED_DIR_NAMES = {
    ".git", "venv", ".venv", "env", "ENV", "confidentials", "data",
    "__pycache__", ".pytest_cache", ".mypy_cache",
}
EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo"}
EXCLUDED_FILE_NAMES = {".DS_Store"}


def _get_version() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("Could not determine the current git commit hash -- is this a git checkout "
              "with at least one commit?", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


def _get_commit_time() -> str:
    """
    The committed date of HEAD, ISO 8601, or "" if it can't be read.

    This is what lets an instance tell a NEWER release from a merely
    different one. Short hashes have no order, so without it the update
    check could only ask "is this the same version?" -- and would offer an
    older release as though it were an upgrade (measured 2026-08-12 on a
    real fresh install). The COMMIT date is used rather than the publish
    time because it orders the code itself, so re-publishing an old commit
    is correctly recognised as old.
    """
    result = subprocess.run(
        ["git", "show", "-s", "--format=%cI", "HEAD"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print("  ⚠ could not read the commit date -- instances will not be able to tell "
              "whether this release is newer than what they are running.", file=sys.stderr)
        return ""
    return result.stdout.strip()


def _iter_package_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if path.suffix in EXCLUDED_FILE_SUFFIXES or path.name in EXCLUDED_FILE_NAMES:
            continue
        yield path, rel


def _build_package(version: str, commit_time: str = "") -> Path:
    from config import UPDATES_DIR

    zip_path = UPDATES_DIR / f"vaulter_ai_{version}.zip"
    if zip_path.exists():
        print(f"  {zip_path.name} already exists — reusing it (this exact code version was "
              f"already packaged).")
        return zip_path

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in _iter_package_files():
            zf.write(path, arcname=str(rel))
            count += 1
        # .git is excluded from every package (EXCLUDED_DIR_NAMES above), so
        # a receiving instance's own git HEAD never moves when an update is
        # applied -- confirmed 2026-07-29 to cause _get_code_version() to
        # report the pre-update commit forever, which made every future
        # check see remote != current and re-stage the SAME already-applied
        # version on a loop, with no way to ever quiet down. Shipping the
        # version as a plain file inside the package itself, instead of only
        # in the external marker JSON, means apply_update.py updates it
        # exactly like any other file -- no git operation needed.
        # Two lines: the hash, then the commit date. Readers take line 1
        # for the version (see mcp_server._get_code_version) and line 2 to
        # tell newer from older (_get_code_build_time). Old readers that
        # take the whole file still match on a plain single-line VERSION,
        # which is why the hash stays first.
        zf.writestr("VERSION",
                    (version + "\n" + commit_time + "\n") if commit_time else version)
    print(f"  Packaged {count} files into {zip_path.name}")
    return zip_path


def _iter_extra_files():
    """
    Every file from the folders beside the program that should ship, with its
    path relative to the install root (so `quick_start/...`, `.claude/...`).
    """
    for top in EXTRA_TOP_LEVEL:
        base = REPO_ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT)
            if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
                continue
            if any(rel.is_relative_to(bad) for bad in EXCLUDED_EXTRA_PATHS):
                continue
            if path.name in EXCLUDED_EXTRA_FILE_NAMES:
                continue
            # From .claude/, ONLY the markdown -- the agents and skills. The
            # same rule build_handoff.py uses, and it earns its keep: this
            # folder also accumulates per-machine run state (a scheduled-task
            # lock file holding a session id and process number was caught by
            # this on its first run). An allowlist by file type means the next
            # such file is excluded automatically rather than after it leaks.
            if rel.parts[0] == ".claude" and path.suffix.lower() != ".md":
                continue
            if path.suffix in EXCLUDED_FILE_SUFFIXES or path.name in EXCLUDED_FILE_NAMES:
                continue
            yield path, rel


def _build_extras_package(version: str) -> Path | None:
    """
    Package quick_start/ and .claude/ separately from the program, and refuse
    to publish if anything confidential got in.

    Why a second package rather than adding these to the main one: an install
    running the code of the day this was written copies EVERY file in the main
    package into its program folder. Adding `quick_start/` there would put a
    copy of the installer inside `system/`, on somebody's real machine, where
    it does nothing -- so the folders that could never reach her would still
    not reach her, just untidily. A separate file is invisible to code that
    does not know to look for it, so nothing happens on an old install at all,
    and the machinery to receive it arrives in the same release.

    Returns None if there is nothing to package.
    """
    from config import UPDATES_DIR

    files = list(_iter_extra_files())
    if not files:
        print("  (no quick_start/ or .claude/ found beside the program -- "
              "publishing the program only)")
        return None

    offenders = sorted({rel.as_posix() for _p, rel in files
                        if rel.name in FORBIDDEN_IN_EXTRAS})
    if offenders:
        print("  ✗ REFUSING to publish: these must never leave this machine, and the "
              "exclusion rules did not catch them:", file=sys.stderr)
        for name in offenders:
            print(f"      {name}", file=sys.stderr)
        sys.exit(1)

    zip_path = UPDATES_DIR / f"vaulter_ai_extras_{version}.zip"
    if zip_path.exists():
        print(f"  {zip_path.name} already exists — reusing it.")
        return zip_path

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, rel in files:
            zf.write(path, arcname=rel.as_posix())
    print(f"  Packaged {len(files)} launcher/agent file(s) into {zip_path.name}")
    return zip_path


def _sign_package(zip_path: Path) -> str:
    """
    Signs the package's SHA-256 hash with the private release-signing key
    and returns the signature, base64-encoded so it's safe to embed in
    the JSON marker. Every instance verifies this against the public key
    (system/release_public_key.pem, tracked) before trusting a download --
    see core/release_signing.py for why this has to be asymmetric.
    """
    from core.release_signing import sign_bytes

    digest = hashlib.sha256(zip_path.read_bytes()).digest()
    signature = sign_bytes(digest, PRIVATE_KEY_PATH)
    return base64.b64encode(signature).decode("ascii")


def _write_marker(channel: str, version: str, zip_filename: str, notes: str,
                  signature: str, commit_time: str = "", force: bool = False,
                  extras_zip_filename: str = "", extras_signature: str = "") -> None:
    from config import UPDATES_DIR
    from core import safe_io

    marker_path = UPDATES_DIR / f"latest_version_{channel}.json"
    safe_io.save_json_atomic(marker_path, {
        "version": version,
        "zip_filename": zip_filename,
        # The launcher/agent-file package, added 2026-08-19. Two extra keys
        # rather than a changed shape on purpose: code that predates them
        # reads the marker with .get() and simply never asks, so an install
        # running older code is unaffected instead of confused.
        "extras_zip_filename": extras_zip_filename,
        "extras_signature": extras_signature,
        "published_at": datetime.now().isoformat(timespec="seconds"),
        # The commit's own date, so an instance can tell whether this is
        # genuinely NEWER than what it runs rather than merely different.
        "commit_time": commit_time,
        # Set only by --force: the deliberate escape hatch for rolling a
        # bad release BACK, which by definition means publishing older code.
        "force": force,
        "notes": notes,
        "signature": signature,
    })
    print(f"  Updated {marker_path.name} — channel \"{channel}\" now points to {version}.")


KEEP_RELEASES = 3


def _prune_old_packages() -> None:
    """
    Delete superseded release zips, keeping the newest KEEP_RELEASES.

    Nothing ever removed these, so they accumulated one per release forever --
    and this folder is synced to every teammate, so everyone downloads all of
    them. Measured 2026-08-11: 10 zips, 2.3 MB, of which 2 were live.

    Three, not one, deliberately. An instance may be part-way through
    downloading a package when a new release lands, and a version still
    referenced by EITHER channel marker must survive regardless of age --
    canary and general routinely point at different versions during a staged
    rollout, which is the whole point of having two channels.
    """
    from config import UPDATES_DIR
    from core import safe_io

    keep_names = set()
    for channel in ("canary", "general"):
        data = safe_io.load_json(UPDATES_DIR / f"latest_version_{channel}.json")
        if not data:
            continue
        # BOTH packages a marker references. Missing the second one would let
        # the newest-three rule below delete a launcher package that a channel
        # is actively pointing at -- the extras zips share the same filename
        # prefix, so they compete for those three slots.
        for key in ("zip_filename", "extras_zip_filename"):
            if data.get(key):
                keep_names.add(data[key])

    all_zips = sorted(UPDATES_DIR.glob("vaulter_ai_*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    # Counted separately for the same reason: three releases means three of
    # each kind, not three files between them.
    extras = [p for p in all_zips if p.name.startswith("vaulter_ai_extras_")]
    main = [p for p in all_zips if not p.name.startswith("vaulter_ai_extras_")]
    keep_names.update(p.name for p in main[:KEEP_RELEASES])
    keep_names.update(p.name for p in extras[:KEEP_RELEASES])

    removed = 0
    for p in all_zips:
        if p.name in keep_names:
            continue
        try:
            p.unlink()
            removed += 1
        except OSError as e:
            # A locked file (OneDrive mid-sync) is not a failure worth aborting
            # a release for -- it just gets cleaned up on the next publish.
            print(f"  (could not remove {p.name}: {e})")
    if removed:
        print(f"  Cleaned up {removed} superseded package(s); kept {len(keep_names)}.")


def publish(notes: str, force: bool = False) -> None:
    print("Vaulter AI — publishing a new version to the CANARY channel")
    version = _get_version()
    commit_time = _get_commit_time()
    print(f"  Version: {version}" + (f"  (committed {commit_time[:16]})" if commit_time else ""))
    if force:
        print("  --force: instances will accept this even if it is OLDER than what "
              "they run. Use only to pull a bad release back.")

    zip_path = _build_package(version, commit_time)
    signature = _sign_package(zip_path)

    # Signed with the same key, and verified the same way on arrival. A launcher
    # package is code that runs on somebody's machine, so it gets exactly the
    # same treatment as the program itself -- never "it's only the installer".
    extras_path = _build_extras_package(version)
    extras_name = extras_path.name if extras_path else ""
    extras_signature = _sign_package(extras_path) if extras_path else ""

    _write_marker("canary", version, zip_path.name, notes, signature, commit_time, force,
                  extras_zip_filename=extras_name, extras_signature=extras_signature)
    _prune_old_packages()

    print()
    print(f"Published. Only instances with VAULTER_UPDATE_CHANNEL=canary will pick this up.")
    print(f"Once confirmed healthy, run: python release.py --promote")


def promote() -> None:
    from config import UPDATES_DIR
    from core import safe_io

    canary_marker = UPDATES_DIR / "latest_version_canary.json"
    canary_data = safe_io.load_json(canary_marker)
    if not canary_data:
        print(f"No canary release found at {canary_marker} — run `python release.py` first.",
              file=sys.stderr)
        sys.exit(1)

    print("Vaulter AI — promoting the current canary release to the GENERAL channel")
    print(f"  Version: {canary_data.get('version')}")
    general_marker = UPDATES_DIR / "latest_version_general.json"
    safe_io.save_json_atomic(general_marker, {**canary_data, "promoted_at": datetime.now().isoformat(timespec="seconds")})
    print(f"  Updated {general_marker.name} — every instance on the default \"general\" "
          f"channel will now pick this up.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", default="", help="Short description of what changed")
    parser.add_argument("--force", action="store_true",
                        help="publish even though this code is OLDER than what instances "
                             "run -- the deliberate way to roll a bad release back")
    parser.add_argument("--promote", action="store_true",
                         help="Promote the current canary release to the general channel")
    args = parser.parse_args()

    if args.promote:
        promote()
    else:
        publish(args.notes, force=args.force)


if __name__ == "__main__":
    main()
