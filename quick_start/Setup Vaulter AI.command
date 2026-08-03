#!/bin/bash
# Double-click this file to run the Vaulter AI setup wizard.
# (No terminal or typed commands needed -- this window just shows its progress.)
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "Python was not found on this computer."
    echo "Install it from https://www.python.org/downloads/macos/ first,"
    echo "then double-click this file again."
    echo
    read -p "Press Enter to close this window..."
    exit 1
fi

# Works in both layouts without needing two versions of this file: the
# development checkout (scripts/ sits beside quick_start/) and the packaged
# handoff folder built by scripts/build_handoff.py (everything tucked into
# system/). Checked in that order; if neither exists the folder is incomplete.
WIZARD="../scripts/setup_wizard.py"
[ -f "$WIZARD" ] || WIZARD="../system/scripts/setup_wizard.py"
if [ ! -f "$WIZARD" ]; then
    echo
    echo "This folder looks incomplete -- the setup files couldn't be found."
    echo "Please ask whoever sent it to you for a fresh copy."
    echo
    read -p "Press Enter to close this window..."
    exit 1
fi

python3 "$WIZARD"
echo
read -p "Press Enter to close this window..."
