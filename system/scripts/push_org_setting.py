"""
push_org_setting.py
--------------------
Vaulter AI — publish a new org-wide setting (typically an API key a new
feature needs) so every staff instance picks it up automatically, the
same way release.py distributes code (Priority 4 in
docs/MULTI_USER_TRANSITION.md) -- but through a SEPARATE channel, on
purpose. See config.py's ORG_SETTINGS_DIR comment for why: a code
update package deliberately never includes confidentials/, so it can't
be reused to carry a secret without reintroducing the exact risk that
exclusion exists to prevent (one person's own filled-in .env shipping
to everyone else).

Run this yourself, once, whenever a new feature needs a new org-wide
value (one key for the whole team, e.g. a shared API key -- NOT a
personal secret). Nothing here needs to be automated; it's a
deliberate "the team needs this value now" action, exactly like
release.py.

Usage:
    python push_org_setting.py --key TEAMS_API_KEY --value abc123 --label "Meeting transcript search"

The --label is what staff will see (in plain English) if they ask Claude
"what feature is that?" -- so keep it brief and non-technical. Examples:
  "Meeting transcript search"
  "Google Places integration"
  "Market analysis tool"

Each instance's scheduler checks ORG_SETTINGS_DIR (same shared
OneDrive location as everything else shared across the team) and
stages any setting whose key isn't already filled in locally. A human
still confirms in a Claude conversation before it's actually written
into their confidentials/.env (see apply_pending_settings in
mcp_server.py) -- staging only, never silently applied, matching
Priority 4's code-update mechanism.

Note on scope: this only fills in a key that's currently BLANK/missing
locally. If someone already has some value set for this key (even an
old one), they're treated as "already configured" and this won't
overwrite it -- so this mechanism distributes new keys, it does not
rotate/replace existing ones. Rotating a shared key still needs a
direct message to whoever already has the old value.

The value briefly sits as plain text in the shared OneDrive folder
(ORG_SETTINGS_DIR) until every instance has picked it up -- the same
trust boundary the team already accepts for screening results and
other shared data, but worth knowing since this one is a literal
credential. Delete the file from ORG_SETTINGS_DIR once you're
confident everyone's synced (there's no automatic expiry).
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))


def publish(key: str, value: str, label: str) -> None:
    from config import ORG_SETTINGS_DIR
    from core import safe_io

    key = key.strip().upper()
    if not key or not value:
        print("Both --key and --value are required.", file=sys.stderr)
        sys.exit(1)

    setting_path = ORG_SETTINGS_DIR / f"{key}.json"
    safe_io.save_json_atomic(setting_path, {
        "key": key,
        "value": value,
        "label": label or key,
        "published_at": datetime.now().isoformat(timespec="seconds"),
    })
    print(f"Published \"{key}\" to {setting_path}.")
    print("Every instance missing this key locally will stage it on its next scheduled "
          "check, then offer to set it up next time someone starts a conversation.")
    print()
    print(f"Once you're confident everyone's picked it up, delete {setting_path.name} "
          f"from the shared org_settings folder (it doesn't expire on its own).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", required=True, help="Env var name, e.g. TEAMS_API_KEY")
    parser.add_argument("--value", required=True, help="The actual value to distribute")
    parser.add_argument("--label", default="", help="Human-friendly description (kept local only, "
                                                       "never shown to staff or sent to Claude)")
    args = parser.parse_args()
    publish(args.key, args.value, args.label)


if __name__ == "__main__":
    main()
