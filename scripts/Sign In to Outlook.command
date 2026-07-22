#!/bin/bash
# Double-click this file to sign into your own Microsoft/Outlook account.
# A short code and a web address will appear below -- open that address
# in your browser and enter the code to finish signing in.
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

python3 main.py auth
echo
read -p "Press Enter to close this window..."
