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

python3 setup_wizard.py
echo
read -p "Press Enter to close this window..."
