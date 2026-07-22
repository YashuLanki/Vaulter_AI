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
import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()

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


def _build_package(version: str) -> Path:
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
    print(f"  Packaged {count} files into {zip_path.name}")
    return zip_path


def _write_marker(channel: str, version: str, zip_filename: str, notes: str) -> None:
    from config import UPDATES_DIR
    import safe_io

    marker_path = UPDATES_DIR / f"latest_version_{channel}.json"
    safe_io.save_json_atomic(marker_path, {
        "version": version,
        "zip_filename": zip_filename,
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "notes": notes,
    })
    print(f"  Updated {marker_path.name} — channel \"{channel}\" now points to {version}.")


def publish(notes: str) -> None:
    print("Vaulter AI — publishing a new version to the CANARY channel")
    version = _get_version()
    print(f"  Version: {version}")

    zip_path = _build_package(version)
    _write_marker("canary", version, zip_path.name, notes)

    print()
    print(f"Published. Only instances with VAULTER_UPDATE_CHANNEL=canary will pick this up.")
    print(f"Once confirmed healthy, run: python release.py --promote")


def promote() -> None:
    from config import UPDATES_DIR
    import safe_io

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
    parser.add_argument("--promote", action="store_true",
                         help="Promote the current canary release to the general channel")
    args = parser.parse_args()

    if args.promote:
        promote()
    else:
        publish(args.notes)


if __name__ == "__main__":
    main()
