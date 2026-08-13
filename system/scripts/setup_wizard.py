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
  3. Checks for Tesseract/Poppler (OCR tools) and, on Windows, installs
     whichever is missing automatically (official sources, per-user, no
     admin rights) -- falling back to manual instructions only if that
     download/install doesn't succeed. See config.py's own auto-detection
     for exactly which folders are checked afterward.
  4. Creates confidentials/.env from confidentials/.env.template if it
     doesn't exist yet. There are no API keys any more, so a blank file
     is a working setup; this step only flags it if the template itself
     is missing.
  5. Finds the TEAM's shared OneDrive folder -- and records where it
     actually is if OneDrive put it somewhere unexpected. Without this a
     teammate silently gets a private empty folder instead (see that
     step's own docstring for why), and portfolio questions come back
     empty with nothing explaining it.
  6. Merges a "vaulter_ai" entry into Claude Desktop's own config file --
     without touching any other entry already in it -- or explains how
     to install Claude Desktop first if it isn't found.
  7. Builds the document-library search index (names/paths only, no file
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


def check_install_location() -> bool:
    """
    Warn before this folder's location gets baked into Claude Desktop's config.

    Step 5 writes this folder's ABSOLUTE path into claude_desktop_config.json, so
    moving, renaming or deleting the folder afterwards silently breaks the
    connection. The realistic way that happens: someone unzips into Downloads,
    runs setup, then tidies up later. Cheaper to say so now than to debug a dead
    connector for them a week later. A warning, never a block -- Downloads is a
    perfectly valid place to keep it if that's a deliberate choice.
    """
    _print_header("0. Where this folder lives")
    home = Path.home()

    # Ask whether this folder is really INSIDE a risky folder, rather than
    # whether its path happens to contain a word. A substring match here told
    # anyone whose Windows username contained "temp" (jtempleton, stemple) that
    # they were running from a temporary folder, and anyone whose path contained
    # "downloads" anywhere above them that they were in Downloads -- a confident
    # cause the code never actually tested, the same class of bug the first real
    # teammate install turned up four times in ten minutes. The .bat launcher
    # already does this properly with its :is_inside helper; this is the same
    # idea in Python.
    def _inside(parent) -> bool:
        if not parent:
            return False
        try:
            PROJECT_ROOT.resolve().relative_to(Path(parent).resolve())
            return True
        except (ValueError, OSError):
            return False

    risky = [
        (home / "Downloads", "your Downloads folder"),
        (os.environ.get("TEMP"), "a temporary folder Windows may clear on its own"),
        (os.environ.get("TMP"), "a temporary folder Windows may clear on its own"),
        (home / "$Recycle.Bin", "the Recycle Bin"),
        (os.environ.get("OneDriveCommercial"), "a OneDrive-synced folder "
                                               "(syncing can move or lock files mid-run)"),
        (os.environ.get("OneDrive"), "a OneDrive-synced folder "
                                     "(syncing can move or lock files mid-run)"),
    ]
    for location, description in risky:
        if _inside(location):
            needle = "onedrive" if "OneDrive" in str(location) else ""
            print(f"  ⚠ This looks like it's running from {description}:")
            print(f"      {PROJECT_ROOT.parent}")
            print("    Setup records this exact location, so moving or deleting the folder")
            print("    later will break the connection to Claude Desktop.")
            if needle == "onedrive":
                # "Move it to Documents" is the obvious advice and the wrong one
                # here: in a Microsoft 365 org, Documents is usually the
                # OneDrive-synced folder, so that lands right back here. Name a
                # concrete local path instead. This also matters because the
                # search index is a ~100MB database that would sync for no
                # reason -- it is local cache and rebuilds in a couple of minutes.
                print("    This folder also holds a large index file that would sync to the")
                print("    cloud for no reason, and syncing a database while it's in use can")
                print("    corrupt it.")
                print(f"    Best to close this window, move the whole 'Vaulter AI' folder to a")
                print(f"    local (non-synced) path such as:")
                print(f"      {Path.home() / 'Vaulter AI'}")
                print("    and run setup again from there.")
            else:
                # Never suggest the folder we are already sitting in -- an
                # earlier version printed "move it to X" where X was X.
                suggestion = home / "Vaulter AI"
                print("    Best to close this window, move the whole 'Vaulter AI' folder somewhere")
                if suggestion.resolve() == PROJECT_ROOT.parent.resolve():
                    print("    permanent and local, outside that folder, and run setup again")
                    print("    from there.")
                else:
                    print(f"    permanent and local, such as {suggestion},")
                    print("    and run setup again from there.")
            print("    Continuing is fine if you meant to keep it here.")
            return False

    print(f"  ✓ {PROJECT_ROOT.parent}")
    print("    Keep the folder here — setup records this location, so moving or renaming")
    print("    it later would break the connection (re-running setup fixes that).")
    return True


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


# Pinned to specific verified releases rather than a "latest" redirect, so this
# keeps working even if a future release changes its asset naming. Both were
# checked live on 2026-08-03: the .exe resolves (HTTP 200, official
# tesseract-ocr GitHub releases), and the zip's own internal layout was
# inspected directly -- it extracts to poppler-<version>/Library/bin, which is
# exactly the pattern config.py's POPPLER_PATH detection already knows to look
# for once extracted under AppData\Local\Programs\poppler.
_TESSERACT_INSTALLER_URL = (
    "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.3/"
    "tesseract-ocr-w64-setup-5.5.3.20260724.exe"
)
_POPPLER_ZIP_URL = (
    "https://github.com/oschwartz10612/poppler-windows/releases/download/"
    "v26.02.0-0/Release-26.02.0-0.zip"
)


def _download_with_progress(url: str, dest: Path) -> bool:
    """Stdlib-only download (no dependency on requests being installed yet)."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=30) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        return True
    except Exception as e:
        print(f"      Download failed: {e}")
        return False


def _install_tesseract_windows() -> bool:
    """
    Downloads the official Windows installer and runs it silently, per-user,
    into the exact folder config.py's TESSERACT_PATH detection already
    searches -- so no config change was needed to make this discoverable.
    """
    username = os.environ.get("USERNAME", "YourName")
    install_dir = Path(r"C:\Users") / username / r"AppData\Local\Programs\Tesseract-OCR"
    installer_path = Path(os.environ.get("TEMP", ".")) / "vaulter_tesseract_installer.exe"

    print("  Downloading Tesseract OCR from the official tesseract-ocr GitHub releases...")
    if _download_with_progress(_TESSERACT_INSTALLER_URL, installer_path):
        print("  Installing (per-user, no admin rights)...")
        try:
            # NSIS installer convention: /S = silent, /D=<dir> = install location.
            # /D must be the last argument and unquoted -- adding quotes ourselves
            # here would double-quote it once subprocess passes it through.
            subprocess.run([str(installer_path), "/S", f"/D={install_dir}"], timeout=180)
        except Exception as e:
            print(f"      Installer didn't run cleanly: {e}")
        finally:
            installer_path.unlink(missing_ok=True)
        if (install_dir / "tesseract.exe").exists():
            return True

    # Second attempt via winget, exactly as the launcher already does for
    # Python. Not redundant: measured 2026-08-12, the direct python.org
    # installer failed on a real machine while winget installed the same
    # version successfully -- so a direct download failing says nothing about
    # whether winget will. A corporate network, proxy or security product can
    # block a raw GitHub download and a silently-run .exe while leaving the
    # Microsoft-signed package path alone. winget ships with Windows 10/11; if
    # it is missing this simply does nothing and we fall through to the manual
    # instructions. Found needed 2026-08-13 when a teammate's Tesseract install
    # failed here and there was no second route to try.
    if shutil.which("winget"):
        print("  Direct install didn't work. Trying again through Windows' own "
              "app installer...")
        # NOT --scope user here, unlike Python. Checked 2026-08-13: neither
        # Tesseract package declares a per-user installer, and winget refuses
        # outright ("no applicable installer") when asked for a scope a package
        # doesn't offer -- so passing it guaranteed the fallback could never
        # work. Let winget choose.
        #
        # Two package ids are tried because they are maintained separately and
        # one may be reachable when the other is not.
        for package in ("UB-Mannheim.TesseractOCR", "tesseract-ocr.tesseract"):
            try:
                subprocess.run(["winget", "install", "--id", package,
                                "--source", "winget", "--silent",
                                "--accept-package-agreements", "--accept-source-agreements"],
                               timeout=600)
            except Exception as e:
                print(f"      {package} didn't work: {e}")
                continue
            if shutil.which("tesseract"):
                return True
        # winget chooses its own location, so ask config.py to look again
        # rather than assuming the folder above.
        import importlib
        import config as _c
        importlib.reload(_c)
        if shutil.which("tesseract") or (_c.TESSERACT_PATH and _c.TESSERACT_PATH != "tesseract"):
            return True

    return False


def _install_poppler_windows() -> bool:
    """
    Downloads the official release zip and extracts it -- Poppler for Windows
    has no installer, just a zip -- into AppData\\Local\\Programs\\poppler,
    which config.py's POPPLER_PATH detection now also searches.
    """
    import zipfile

    username = os.environ.get("USERNAME", "YourName")
    extract_root = Path(r"C:\Users") / username / r"AppData\Local\Programs\poppler"
    zip_path = Path(os.environ.get("TEMP", ".")) / "vaulter_poppler.zip"

    print("  Downloading Poppler from the official poppler-windows GitHub releases...")
    if not _download_with_progress(_POPPLER_ZIP_URL, zip_path):
        return False

    print("  Extracting (per-user, no admin rights)...")
    try:
        extract_root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_root)
    except Exception as e:
        print(f"      Couldn't extract the download: {e}")
        return False
    finally:
        zip_path.unlink(missing_ok=True)

    return any((d / "Library" / "bin" / "pdftoppm.exe").exists()
               for d in extract_root.glob("poppler*") if d.is_dir())


def check_ocr_tools() -> bool:
    _print_header("3. OCR tools (Tesseract + Poppler)")
    # Imported here, not at module load time -- config.py's own imports
    # (dotenv, etc.) only need to succeed AFTER step 2 has installed them.
    import config
    import importlib

    ok = True
    if shutil.which("tesseract") or (config.TESSERACT_PATH and config.TESSERACT_PATH != "tesseract"):
        print(f"  ✓ Tesseract OCR found: {config.TESSERACT_PATH}")
    elif sys.platform == "win32":
        print("  ⚠ Tesseract OCR was not found. Installing it now (official installer, "
              "per-user, no admin rights)...")
        if _install_tesseract_windows():
            importlib.reload(config)
            print(f"  ✓ Tesseract OCR installed: {config.TESSERACT_PATH}")
        else:
            ok = False
            # Say what is actually lost, not just that a step failed. Measured on
            # a real teammate's machine 2026-08-13: this printed a bare failure
            # plus a GitHub wiki link, which reads as "setup is broken" to a
            # non-technical person. It is not -- everything else works, and this
            # affects only scanned pages.
            print("  ⚠ Tesseract couldn't be installed automatically, and that is OK to")
            print("    leave for now -- nothing else is affected.")
            print("    What it changes: pages that are PHOTOCOPIES or SCANS of paper")
            print("    can't be read. Ordinary PDFs, Word, Excel and everything else are")
            print("    completely unaffected, and the rest of setup carries on normally.")
            print("    To add it later (no admin rights needed), either run this in a")
            print("    Command Prompt:")
            print("      winget install UB-Mannheim.TesseractOCR")
            print("    or download it from https://github.com/UB-Mannheim/tesseract/wiki")
    else:
        ok = False
        print("  ⚠ Tesseract OCR was not found. Scanned/image-only PDF pages won't be "
              "readable until it's installed. No admin rights needed:")
        print("      Mac: brew install tesseract")

    if config.POPPLER_PATH:
        print(f"  ✓ Poppler found: {config.POPPLER_PATH}")
    elif sys.platform == "win32":
        print("  ⚠ Poppler was not found. Installing it now (official release, per-user, "
              "no admin rights)...")
        if _install_poppler_windows():
            importlib.reload(config)
            print(f"  ✓ Poppler installed: {config.POPPLER_PATH}")
        else:
            ok = False
            print("  ⚠ Couldn't install Poppler automatically. You can install it yourself "
                  "instead -- no admin rights needed:")
            print("      https://github.com/oschwartz10612/poppler-windows/releases "
                  "(unzip it into %LOCALAPPDATA%\\Programs\\poppler)")
    else:
        ok = False
        print("  ⚠ Poppler was not found. Scanned/image-only PDF pages won't be readable "
              "until it's installed. No admin rights needed:")
        print("      Mac: brew install poppler")

    # Register both tools on the USER PATH so they're callable by name from any
    # shell, not only from code that imports config.py's explicit paths. Found
    # 2026-08-04: poppler was installed but findable by nothing -- every shell
    # that tried `pdftoppm` failed. Per-user registry value only (never the
    # system PATH, no admin rights); written via the .NET API rather than
    # `setx`, which silently truncates PATH at 1024 characters.
    if sys.platform == "win32":
        _register_ocr_tools_on_user_path(config)

    if not ok:
        print("  (Digital-text PDFs are completely unaffected either way -- only scanned/"
              "image-only pages need these tools.)")
    return ok


def _register_ocr_tools_on_user_path(config) -> None:
    """
    Add the OCR tool folders to the per-user PATH (HKCU\\Environment).

    Written with winreg rather than by shelling out: `setx` silently truncates
    PATH at 1024 characters, and passing paths as arguments to
    `powershell -Command` does NOT populate $args -- they get appended to the
    script text and PowerShell tries to execute them as commands. That failed
    loudly on stderr while returning nothing on stdout, so a stdout-only check
    read it as a successful no-op. winreg avoids the whole quoting problem.

    Never touches the machine-wide PATH, so no admin rights are needed.
    """
    import winreg
    dirs = []
    if config.POPPLER_PATH:
        dirs.append(str(config.POPPLER_PATH))
    if config.TESSERACT_PATH and config.TESSERACT_PATH != "tesseract":
        dirs.append(str(Path(config.TESSERACT_PATH).parent))
    if not dirs:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            parts = [p for p in current.split(";") if p]
            existing = {p.rstrip("\\").lower() for p in parts}
            added = [d for d in dirs if d.rstrip("\\").lower() not in existing]
            if not added:
                return
            # Preserve REG_EXPAND_SZ if that's what was there -- rewriting an
            # expandable PATH as a plain string would freeze any %VAR% in it.
            winreg.SetValueEx(key, "Path", 0, kind or winreg.REG_EXPAND_SZ,
                              ";".join(parts + added))
        print(f"  ✓ Added to your PATH so other tools can find them: {'; '.join(added)}")
        print("    (Takes effect in newly opened windows.)")
    except OSError as e:
        # Cosmetic hardening only -- the system itself never needs this
        # (extract.py passes explicit paths). Never fail setup over it, but
        # say so rather than looking like it worked.
        print(f"  (Couldn't add the OCR tools to your PATH: {e} -- harmless, "
              f"Vaulter AI uses their full paths directly.)")


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


def _looks_like_team_folder(d: Path) -> bool:
    """A shared folder with real content in it, not an empty auto-created one."""
    try:
        return d.is_dir() and any(d.rglob("*.*"))
    except OSError:
        return False


def _hunt_for_shared_folder(onedrive_root: Path, corpus_dir) -> Path | None:
    """
    Look for the team's shared folder wherever OneDrive actually put it.

    "Add shortcut to My files" usually lands at the OneDrive root under the
    original name -- but not always: if this installer already created an empty
    "Vaulter AI Shared" (it does, on first run), OneDrive renames the shortcut
    to avoid the clash, and some setups nest it a level down instead. Rather
    than insist on one exact path, find a folder that looks like the real one
    and record it.

    Bounded to two levels deliberately, and the document library is skipped
    outright: it holds a huge number of OneDrive placeholder files, and walking it here
    would take minutes and hydrate files nobody asked for.
    """
    name = "Vaulter AI Shared"
    hits = []
    try:
        for lvl1 in onedrive_root.iterdir():
            if not lvl1.is_dir():
                continue
            if corpus_dir and lvl1.resolve() == Path(corpus_dir).resolve():
                continue  # never walk the firm's document library
            if lvl1.name.startswith(name) and _looks_like_team_folder(lvl1):
                hits.append(lvl1)
                continue
            try:
                for lvl2 in lvl1.iterdir():
                    if lvl2.is_dir() and lvl2.name.startswith(name) \
                            and _looks_like_team_folder(lvl2):
                        hits.append(lvl2)
            except OSError:
                continue
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def check_shared_folder() -> bool:
    """
    Make sure this machine can see the TEAM's shared folder, not a private
    empty one.

    Why this step exists: without it a teammate silently gets an empty folder
    that this system creates itself, and everything looks connected while being
    completely isolated -- no portfolio, no shared CoStar exports. Better to say
    so during setup than let them find out later.

    The folder now lives INSIDE the firm's document library (moved 2026-08-03),
    so it arrives with the library rather than needing to be shared separately.
    That means when it IS missing, the cause is almost always the library not
    syncing -- not a missing share. This docstring said the opposite until
    2026-08-12, and so did the message this function printed.
    """
    _print_header("5. The team's shared folder")
    import config

    if not config.SHARED_DIR_IS_FALLBACK and _looks_like_team_folder(config.SHARED_DIR):
        print(f"  ✓ Found, with team data in it:\n      {config.SHARED_DIR}")
        return True

    found = None
    if config.ONEDRIVE_ROOT:
        found = _hunt_for_shared_folder(config.ONEDRIVE_ROOT, config.CORPUS_DIR)

    if found:
        # Record it so every later run goes straight there, whatever OneDrive
        # decided to call it.
        env_path = PROJECT_ROOT / "confidentials" / ".env"
        try:
            existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            if "VAULTER_SHARED_DIR=" not in existing:
                with open(env_path, "a", encoding="utf-8") as f:
                    f.write(f"\n# Set by the setup wizard: where this machine actually\n"
                            f"# sees the team's shared folder.\n"
                            f"VAULTER_SHARED_DIR={found}\n")
            print(f"  ✓ Found the team's folder at a different name/location:\n"
                  f"      {found}\n"
                  f"    Recorded it so Vaulter AI uses it from now on.")
            return True
        except OSError as e:
            print(f"  ⚠ Found it at {found} but couldn't save that setting ({e}).")
            return False

    # The advice here used to be: get someone to share the folder with you, then
    # click "Add shortcut to My files" on onedrive.com. That was true when the
    # shared folder was an ordinary folder in one person's OneDrive. It moved
    # INSIDE the document library on 2026-08-03 precisely so it reaches everyone
    # automatically and that manual step disappeared -- so the instructions had
    # been sending people to perform a step that no longer exists, for a cause
    # that was no longer the cause. Same failure as the Claude Desktop message:
    # confidently naming a reason nothing had actually tested.
    print("  ⚠ This machine can't see the team's shared folder yet.")
    print("    Everything else still works — but portfolio questions will come back")
    print("    empty, and you won't see the team's CoStar exports, until it's fixed.")
    print()
    if config.CORPUS_UNRESOLVED_REASON == "ambiguous":
        # Found live on a real teammate's machine, 2026-08-13: this branch used to
        # print "that library isn't syncing to this computer yet", which was FALSE
        # for her -- two libraries were syncing perfectly well and setup simply
        # refused to pick between them. She would have gone to OneDrive, seen
        # everything syncing, and had nowhere to go. "Library not available" has
        # several causes; say which one actually happened.
        print("    The reason is NOT that your files are missing. This computer syncs")
        print("    more than one SharePoint library, and Vaulter AI won't guess which")
        print("    one is the firm's — so it hasn't looked inside any of them yet.")
        print("    Step 7 below has the one-line fix; do that and this clears too.")
    elif not config.CORPUS_AVAILABLE:
        print("    The likely reason: the team's folder lives INSIDE the firm's document")
        print("    library, and that library isn't syncing to this computer yet. Fix that")
        print("    first (step 7 below says how) and this usually fixes itself.")
    else:
        # Found on Ava's machine 2026-08-13. This used to say the shared folder
        # had probably not been created yet and to ask Yashu to confirm it
        # exists -- it does, and has for months. What had actually happened is
        # that she was syncing a DIFFERENT SharePoint library (the site's
        # default "Documents" one) and not the firm's document library at all,
        # so the folder was never going to be there. Telling her nothing on her
        # side was broken was the exact wrong steer: there was one thing to do,
        # and it was on her side. Same failure as every other message in this
        # family -- one symptom, several causes, and it picked the wrong one.
        print(f"    A SharePoint library is syncing, but it has no 'Vaulter AI Shared'")
        print(f"    folder inside it:")
        print(f"      {config.CORPUS_DIR}")
        print()
        print("    Most likely this is a DIFFERENT library from the firm's document")
        print("    library. An organisation usually has several, and Vaulter AI needs")
        print("    the specific one the firm keeps its property documents in.")
        print("    In OneDrive, check that the firm's document library is set to sync")
        print("    on this computer (ask Yashu which one it is), let it finish, then")
        print("    double-click \"Setup Vaulter AI\" again.")
        print()
        print("    Less likely: this IS the right library and it simply hasn't finished")
        print("    syncing yet — in which case waiting and re-running setup is enough.")
    print()
    print("    Then double-click 'Setup Vaulter AI' again — it will find it")
    print("    automatically, wherever OneDrive puts it.")
    return False


def _claude_desktop_config_path() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return None
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def setup_claude_desktop() -> bool:
    _print_header("6. Claude Desktop connection")
    config_path = _claude_desktop_config_path()
    if config_path is None:
        print("  ✗ Could not determine where Claude Desktop's config file lives on this OS.")
        return False

    if not config_path.parent.exists():
        # The folder being checked is Claude Desktop's SETTINGS folder, which it
        # creates the first time it RUNS -- not when it is installed. The program
        # itself lives somewhere else entirely. So "this folder is missing" means
        # "never opened", and the old wording said "doesn't appear to be
        # installed", which is both wrong and maddening for someone looking at
        # the app on their own machine. Reported by a real teammate 2026-08-12.
        installed_at = next(
            (p for p in (
                Path(os.environ.get("LOCALAPPDATA", "")) / "AnthropicClaude",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Claude",
                Path(os.environ.get("PROGRAMFILES", "")) / "Claude",
            ) if str(p) and p.exists()),
            None,
        )
        if installed_at:
            # It IS installed -- just never opened. Write the settings file
            # anyway: Claude Desktop reads it at startup, so doing it now means
            # one fewer round trip for someone who just wants this to work.
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                print(f"  ⚠ Claude Desktop is installed, but its settings folder could not "
                      f"be created ({e}). Open Claude Desktop once, then re-run this wizard.")
                return False
            print("  Claude Desktop is installed but hasn't been opened yet, so its")
            print("  settings folder didn't exist. Created it and continuing.")
        else:
            print("  ⚠ Claude Desktop hasn't been opened on this computer yet, and this "
                  "wizard couldn't find it installed either.")
            print("     If you HAVE installed it: open Claude Desktop once (sign in), then")
            print("     double-click \"Setup Vaulter AI\" again -- that is all it needs.")
            print("     If you haven't: install it from https://claude.ai/download first.")
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
    _print_header("7. Index the document library")

    try:
        import config
    except Exception as e:
        print(f"  ⚠ Could not load configuration: {e}")
        return False

    if not config.CORPUS_AVAILABLE:
        # CORPUS_AVAILABLE is False for three different reasons and the old
        # message asserted one of them. For a machine syncing two SharePoint
        # libraries it was flatly wrong -- the library IS there, we just
        # refused to guess which. Telling someone their files are missing when
        # they can see them is the same mistake the Claude Desktop step made
        # (a teammate hit that one on 2026-08-12), so each cause now gets the
        # instruction that actually resolves it.
        if config.ONEDRIVE_ROOT is None:
            print("  ⚠ OneDrive doesn't look like it's set up on this computer yet.")
            print("     Open OneDrive, sign in with your work account, and let it finish")
            print("     its first sync. Then double-click \"Setup Vaulter AI\" again.")
        elif config.CORPUS_DIR is None:
            # Detection found either nothing, or more than one and refused.
            try:
                candidates = [d for d in config.ONEDRIVE_ROOT.iterdir()
                              if d.is_dir() and " - " in d.name
                              and d.name != config.SHARED_SUBFOLDER
                              and not d.name.lower().startswith(config._PERSONAL_ONEDRIVE_FOLDERS)]
            except OSError:
                candidates = []
            if len(candidates) > 1:
                print(f"  ⚠ This computer is syncing {len(candidates)} SharePoint libraries, so "
                      f"setup can't tell which one holds the firm's documents.")
                print("     Nothing is wrong with your files -- it just won't guess.")
                print("     This is a one-line fix. Ask Yashu for the library's folder name,")
                print("     then open this file in Notepad:")
                print(f"       {PROJECT_ROOT / 'confidentials' / '.env'}")
                print("     add this line at the bottom (replacing the part after the '='):")
                print("       VAULTER_CORPUS_SUBFOLDER=the folder name Yashu gives you")
                print("     save it, and double-click \"Setup Vaulter AI\" again.")
                # Deliberately still does NOT print the candidate folder names.
                # That looked like unhelpful caution until 2026-08-13, when a
                # teammate photographed this exact screen and sent it on -- which
                # is precisely the leak the omission prevents. The name reaches
                # her privately instead.
            else:
                print("  ⚠ The firm's document library isn't syncing to this computer yet.")
                print("     In OneDrive, make sure the firm's document library is set to")
                print("     sync (not just visible on the website), let it finish, then")
                print("     double-click \"Setup Vaulter AI\" again.")
        else:
            print("  ⚠ The document library was found, but the folder isn't readable.")
            print(f"     Expected it at: {config.CORPUS_DIR}")
            print("     This usually means OneDrive is still setting it up. Wait for it to")
            print("     finish, then double-click \"Setup Vaulter AI\" again.")
        return False

    print(f"  Library: {config.CORPUS_DIR}")
    print("  Reading file and folder NAMES only — no documents are downloaded.")
    print("  This takes a couple of minutes. Leave the window open.")
    print()
    try:
        from corpus import build_index
        result = build_index()
        print(f"  ✓ Indexed {result['file_count']:,} files in {result['build_seconds']:.0f}s.")
    except Exception as e:
        print(f"  ⚠ Could not build the index: {e}")
        print("     You can try again later by re-running this wizard, or by running")
        print("     'python system/main.py index-corpus' from this folder.")
        return False

    _schedule_daily_refresh()
    return True


def _schedule_daily_refresh() -> None:
    """
    Register a daily Windows task that rebuilds the file list.

    Why this has to exist: the "this summary may be out of date" warning
    compares a summary's own date against the file list. If the list is never
    rebuilt, it freezes at install day and the warning quietly stops warning --
    the failure mode is silence, not an error, which is the worst kind here.

    Daily (was weekly, and monthly before that) because measurement beat the
    guess: on one live deal in its inspection period, the list rebuilt one day
    earlier already missed 24 documents, and a week's worth ran past a hundred.
    Entitlement work moves in months and a weekly rhythm suited it fine, but a
    deal under contract moves in hours -- a contract amendment landed and was
    signed the same morning it was found. Costs nothing on disk at any cadence:
    it reads names and dates only, never opens a document.

    A scheduled task rather than a thread or a subagent. mcp_server.py runs no
    background threads (see CLAUDE.md), and a subagent only exists inside a live
    conversation -- nothing dispatches one on a timer, and Claude Desktop does
    not load them at all. The OS is the only thing here that actually runs on
    its own.

    Per-user, no admin rights. Never fails setup: a machine without this still
    works, it just needs the wizard re-run occasionally.
    """
    if sys.platform != "win32":
        return
    import subprocess

    old_tasks = ("Vaulter AI - Monthly document list refresh",
                 "Vaulter AI - Weekly document list refresh")
    task = "Vaulter AI - Daily document list refresh"
    target = PROJECT_ROOT / "main.py"
    if not target.exists():
        return

    # Remove every earlier-named task a prior install of this wizard left
    # behind -- otherwise each rename leaves another one registered alongside,
    # orphaned and silently doing nothing useful. This has now been renamed
    # twice (monthly -> weekly -> daily), so clean up both older names.
    for old_task in old_tasks:
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f'Unregister-ScheduledTask -TaskName "{old_task}" -Confirm:$false '
                 f'-ErrorAction SilentlyContinue'],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            pass  # best-effort cleanup; a stray old task isn't worth failing setup over

    # pythonw.exe, NOT python.exe. Measured 2026-08-04: with python.exe the
    # task registered fine, reported "Ready", then died on every run with
    # 0xC000013A (console-close) having rebuilt nothing. The console window a
    # scheduled task opens is torn down under it. pythonw has no console, so
    # there is nothing to close. This failed silently -- the task looked
    # healthy while doing nothing -- which is exactly the shape of bug that
    # would have quietly disabled the staleness warning for months.
    runner = Path(sys.executable).with_name("pythonw.exe")
    if not runner.exists():
        runner = Path(sys.executable)

    # Values reach PowerShell via environment variables, never spliced into
    # the command text. Measured 2026-08-04 on a real end-to-end install: the
    # previous version passed them as command-line arguments, and the
    # apostrophe in "Vaulter AI's" broke PowerShell's parsing -- the task
    # silently failed to register while every other setup step succeeded.
    ps = (
        "$a = New-ScheduledTaskAction -Execute $env:VLT_RUNNER -Argument $env:VLT_ARG; "
        "$t = New-ScheduledTaskTrigger -Daily -DaysInterval 1 -At 7am; "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable "
        "-ExecutionTimeLimit (New-TimeSpan -Hours 2) -MultipleInstances IgnoreNew; "
        "Register-ScheduledTask -TaskName $env:VLT_TASK -Action $a -Trigger $t -Settings $s "
        "-Description $env:VLT_DESC -Force | Out-Null"
    )
    env = dict(os.environ,
               VLT_RUNNER=str(runner),
               VLT_ARG=f'"{target}" index-corpus',
               VLT_TASK=task,
               VLT_DESC=("Rebuilds Vaulter AI's list of documents in the firm's library. "
                         "Reads file names and dates only - never opens or downloads "
                         "documents, so it uses no disk space."))
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if r.returncode == 0:
            print()
            print("  ✓ Scheduled a daily refresh of the document list (every day, 7am).")
            print("    It reads file names only and never opens a document, so it costs")
            print("    no disk space. This is what keeps the \"this summary may be out")
            print("    of date\" warning honest, and lets a new document on a live deal")
            print("    show up the next morning instead of days later.")
            print(f"    Remove it any time from Windows Task Scheduler: \"{task}\".")
        else:
            print(f"\n  (Could not schedule the daily refresh: "
                  f"{(r.stderr or r.stdout).strip()[:120]})")
            print("   Not a problem today -- re-run this setup occasionally instead.")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"\n  (Could not schedule the daily refresh: {e})")
        print("   Not a problem today -- re-run this setup occasionally instead.")


def main() -> None:
    print("Vaulter AI — Setup Wizard")
    print(f"Project root: {PROJECT_ROOT}")

    # Deliberately first, and deliberately non-blocking: it's advice about where
    # the folder lives, and it's most useful BEFORE step 5 bakes that location
    # into Claude Desktop's config.
    results = {
        "Folder in a permanent location": check_install_location(),
    }

    results["Python version"] = check_python_version()
    if not results["Python version"]:
        _print_summary(results)
        sys.exit(1)

    results["Dependencies installed"] = install_dependencies()
    if not results["Dependencies installed"]:
        _print_summary(results)
        sys.exit(1)

    results["OCR tools (optional -- scanned pages only)"] = check_ocr_tools()
    results["Credentials ready"] = setup_env_file()
    results["Team shared folder"] = check_shared_folder()
    results["Claude Desktop connected"] = setup_claude_desktop()

    results["Document library indexed"] = build_corpus_index()

    _print_summary(results)


def _print_summary(results: dict) -> None:
    _print_header("Summary")
    for step, ok in results.items():
        print(f"  {'✓' if ok else '⚠'} {step}")

    # OCR is the one step whose failure blocks nothing. Lumping it in with the
    # real problems made a teammate's setup read as more broken than it was.
    blocking = {k: v for k, v in results.items() if not k.startswith("OCR tools")}

    if all(blocking.values()) and not all(results.values()):
        print()
        print("The only thing not set up is OCR, which is optional -- it affects scanned")
        print("or photocopied pages only. Everything else is ready. Fully quit and reopen")
        print("Claude Desktop and start a new conversation.")
        return

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
