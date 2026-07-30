"""
setup_wizard.py
----------------
Vaulter AI — guided setup wizard (Priority 3 in docs/MULTI_USER_TRANSITION.md).

Run this once when setting up a new machine. The non-technical way:
double-click "Setup Vaulter AI.command" (Mac) or "Setup Vaulter AI.bat"
(Windows) in this same folder -- no terminal needed. The manual/
troubleshooting way, if you'd rather:

    python setup_wizard.py

What it does, in order — each step is checked and reported in plain
English rather than assumed to have succeeded:
  1. Checks the Python version is one dependencies are known to work on.
  2. Installs Python dependencies from requirements.txt.
  3. Reports whether Tesseract/Poppler (OCR tools) were found, and how to
     install them (non-admin, per-user methods only — see config.py's own
     auto-detection, which this step just reports on).
  4. Creates confidentials/.env from confidentials/.env.template if it
     doesn't exist yet. There are no API keys any more, so a blank file
     is a working setup; this step only flags it if the template itself
     is missing.
  5. Merges a "vaulter_ai" entry into Claude Desktop's own config file --
     without touching any other entry already in it -- or explains how
     to install Claude Desktop first if it isn't found.
  6. Builds the document-library search index (names/paths only, no file
     contents) so search works from the first conversation.

Per Priority 3's design, this is now the ENTIRE setup for a non-technical
user: one double-click, ending with "fully quit and reopen Claude Desktop."
There is no sign-in step of any kind -- email was dropped in the 2026-07
rebuild, and document access rides on OneDrive syncing the library to disk,
which the user is already signed into for OneDrive itself.

This script deliberately does NOT try to install Python itself (that's
a separate, one-time step -- see README.md's Setup section for the
per-user "install for me only" links that need no admin rights) or
create a virtual environment for you -- it assumes you're already
running it with the Python interpreter (system or venv) you intend to
use for this project, matching how every other command in this project
is invoked (`python main.py ...`).
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows consoles default to a legacy codepage (e.g. cp1252), not UTF-8 --
# this wizard prints ✓/⚠/✗ throughout, which crashes with UnicodeEncodeError
# on that default. This is THE non-technical onboarding path (double-clicked
# via "Setup Vaulter AI.bat"), so it must degrade gracefully, not crash on
# its very first status line. reconfigure() exists on both streams since
# Python 3.7; guarded in case stdout/stderr don't support it in some
# environment.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

# Dependencies in requirements.txt are known to have prebuilt wheels
# (no slow/fragile from-source compiles) on these Python versions across
# Windows and Mac. A much newer interpreter (e.g. whatever the latest
# release is at any given time) may not have prebuilt wheels yet for the
# compiled packages here (pandas, Pillow), forcing a slow or outright
# broken source build -- exactly the "author's bleeding-edge Python" risk
# flagged in docs/MULTI_USER_TRANSITION.md. This is much less likely than
# it was before the rebuild dropped chromadb/onnxruntime.
RECOMMENDED_PYTHON = [(3, 11), (3, 12)]
MIN_PYTHON = (3, 10)  # this codebase uses `X | None` type hints (3.10+)


def _print_header(title: str) -> None:
    print()
    print("=" * 64)
    print(title)
    print("=" * 64)


def check_python_version() -> bool:
    _print_header("1. Python version")
    version = sys.version_info[:2]
    if version < MIN_PYTHON:
        print(f"  ✗ Python {version[0]}.{version[1]} is too old — this project needs at "
              f"least Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}. Install a newer Python first "
              f"(see README.md's Setup section) and re-run this wizard with it.")
        return False
    if version not in RECOMMENDED_PYTHON:
        recommended = " or ".join(f"{v[0]}.{v[1]}" for v in RECOMMENDED_PYTHON)
        print(f"  ⚠ Python {version[0]}.{version[1]} isn't one of the versions this project's "
              f"dependencies are best-tested against ({recommended}). It will likely still "
              f"work, but if `pip install` below fails or is unusually slow for any package, "
              f"that's the most likely reason — installing Python {recommended} instead "
              f"usually fixes it.")
        return True
    print(f"  ✓ Python {version[0]}.{version[1]} — good.")
    return True


def install_dependencies() -> bool:
    _print_header("2. Python dependencies")
    requirements = PROJECT_ROOT / "requirements.txt"
    if not requirements.exists():
        print(f"  ✗ Could not find {requirements} — is this wizard being run from the "
              f"project's root folder?")
        return False
    print("  Installing from requirements.txt (this can take a few minutes)...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(requirements)],
    )
    if result.returncode != 0:
        print("  ✗ pip install failed — see the output above for which package failed and "
              "why. A common fix is installing one of the recommended Python versions above "
              "and re-running this wizard with it.")
        return False
    print("  ✓ All Python dependencies installed.")
    return True


def check_ocr_tools() -> bool:
    _print_header("3. OCR tools (Tesseract + Poppler)")
    # Imported here, not at module load time -- config.py's own imports
    # (dotenv, etc.) only need to succeed AFTER step 2 has installed them.
    import config

    ok = True
    if shutil.which("tesseract") or (config.TESSERACT_PATH and config.TESSERACT_PATH != "tesseract"):
        print(f"  ✓ Tesseract OCR found: {config.TESSERACT_PATH}")
    else:
        ok = False
        print("  ⚠ Tesseract OCR was not found. Scanned/image-only PDF pages won't be "
              "readable until it's installed. No admin rights needed:")
        if sys.platform == "win32":
            print("      Windows: https://github.com/UB-Mannheim/tesseract/wiki "
                  "(use the per-user install option)")
        else:
            print("      Mac: brew install tesseract")

    if config.POPPLER_PATH:
        print(f"  ✓ Poppler found: {config.POPPLER_PATH}")
    else:
        ok = False
        print("  ⚠ Poppler was not found. Scanned/image-only PDF pages won't be readable "
              "until it's installed. No admin rights needed:")
        if sys.platform == "win32":
            print("      Windows: https://github.com/oschwartz10612/poppler-windows/releases "
                  "(just unzip it anywhere -- nothing to install or elevate)")
        else:
            print("      Mac: brew install poppler")

    if not ok:
        print("  (Digital-text PDFs are completely unaffected either way -- only scanned/"
              "image-only pages need these tools.)")
    return ok


def setup_env_file() -> bool:
    _print_header("4. Credentials (confidentials/.env)")
    secrets_dir = PROJECT_ROOT / "confidentials"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    env_path = secrets_dir / ".env"
    template_path = secrets_dir / ".env.template"

    if not env_path.exists():
        if not template_path.exists():
            print(f"  ✗ Neither {env_path} nor {template_path} exists — cannot set up "
                  f"credentials automatically. See README.md's Setup section to create "
                  f"confidentials/.env by hand.")
            return False
        shutil.copy(template_path, env_path)
        print(f"  ✓ Created {env_path} from the template.")
    else:
        print(f"  ✓ {env_path} already exists — leaving it untouched.")

    # Report which organization-wide values are still blank placeholders,
    # without ever printing the actual values (even placeholder ones,
    # since a real value could be sitting in this file already).
    # Nothing is required any more -- the system calls no paid API. The one
    # optional key powers the proximity export only, so a blank .env is a
    # perfectly good setup and must not fail this step.
    org_wide_keys = []
    values = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()

    blank = [k for k in org_wide_keys if not values.get(k)]
    if blank:
        print(f"  ⚠ These organization-wide values are still blank: {', '.join(blank)}")
        print("     These are shared team values (not per-person secrets) -- ask whoever "
              "manages this project's credentials to fill in confidentials/.env.template "
              "before distributing this installer, or fill them into confidentials/.env "
              "directly on this machine. See that template file's own comments for details.")
        return False
    print("  ✓ Organization-wide values are filled in — nothing left to do here.")
    return True


def _claude_desktop_config_path() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def setup_claude_desktop() -> bool:
    _print_header("5. Claude Desktop connection")
    config_path = _claude_desktop_config_path()
    if config_path is None:
        print("  ✗ Could not determine where Claude Desktop's config file lives on this OS.")
        return False

    if not config_path.parent.exists():
        print(f"  ⚠ Claude Desktop doesn't appear to be installed yet (expected its folder "
              f"at {config_path.parent}). Install Claude Desktop first from "
              f"https://claude.ai/download, open it once, then re-run this wizard.")
        return False

    from core import safe_io

    existing = safe_io.load_json(config_path) if config_path.exists() else {}
    existing.setdefault("mcpServers", {})
    main_py = str(PROJECT_ROOT / "main.py")
    existing["mcpServers"]["vaulter_ai"] = {
        "command": sys.executable,
        "args": [main_py, "mcp"],
    }
    safe_io.save_json_atomic(config_path, existing)
    print(f"  ✓ Added/updated the \"vaulter_ai\" entry in {config_path}")
    print("     Every other entry already in that file (other MCP servers, preferences) "
          "was left untouched.")
    print("     Restart Claude Desktop (fully quit and reopen) for this to take effect.")
    return True


def build_corpus_index() -> bool:
    """
    Build the document-library index -- the last step, because it's the slow
    one (a couple of minutes over ~500k files) and everything before it is
    quick.

    This replaces what used to be the Outlook sign-in step. There is no
    per-person sign-in any more: the library reaches this machine through
    OneDrive, which the user is already signed into.
    """
    _print_header("6. Index the document library")

    try:
        import config
    except Exception as e:
        print(f"  ⚠ Could not load configuration: {e}")
        return False

    if not config.CORPUS_AVAILABLE:
        print(f"  ⚠ The document library isn't on this machine yet.")
        print(f"     Expected it at: {config.CORPUS_DIR}")
        print("     Open OneDrive, sign in with your Vaulter account, and make sure the")
        print("     'Vaulter LLC - shaw' library is set to sync. Then re-run this wizard.")
        return False

    print(f"  Library: {config.CORPUS_DIR}")
    print("  Reading file and folder NAMES only — no documents are downloaded.")
    print("  This takes a couple of minutes. Leave the window open.")
    print()
    try:
        from corpus import build_index
        result = build_index()
        print(f"  ✓ Indexed {result['file_count']:,} files in {result['build_seconds']:.0f}s.")
        return True
    except Exception as e:
        print(f"  ⚠ Could not build the index: {e}")
        print("     You can try again later by re-running this wizard, or by running")
        print("     'python main.py index-corpus' from this folder.")
        return False


def main() -> None:
    print("Vaulter AI — Setup Wizard")
    print(f"Project root: {PROJECT_ROOT}")

    results = {
        "Python version": check_python_version(),
    }
    if not results["Python version"]:
        _print_summary(results)
        sys.exit(1)

    results["Dependencies installed"] = install_dependencies()
    if not results["Dependencies installed"]:
        _print_summary(results)
        sys.exit(1)

    results["OCR tools found"] = check_ocr_tools()
    results["Credentials ready"] = setup_env_file()
    results["Claude Desktop connected"] = setup_claude_desktop()

    results["Document library indexed"] = build_corpus_index()

    _print_summary(results)


def _print_summary(results: dict) -> None:
    _print_header("Summary")
    for step, ok in results.items():
        print(f"  {'✓' if ok else '⚠'} {step}")

    if all(results.values()):
        print()
        print("Everything is set up -- there is nothing left to do. Fully quit and reopen")
        print("Claude Desktop and start a new conversation -- it will connect to your own")
        print("local Vaulter AI instance automatically.")
    else:
        print()
        print("Some steps need attention — see the ⚠/✗ notes above for exactly what to")
        print("do next. Re-run this wizard after fixing them; it's safe to run more than")
        print("once (it won't overwrite anything already set up).")


if __name__ == "__main__":
    main()
