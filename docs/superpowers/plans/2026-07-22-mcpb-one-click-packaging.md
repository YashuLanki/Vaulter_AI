# One-Click .mcpb Desktop Extension Packaging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package Vaulter AI as a one-click Claude Desktop Extension (`.mcpb`) so a staff member
downloads one file, double-clicks it, and Claude Desktop installs Python, every dependency, and
the OCR tools automatically — no terminal, no separate wizard, at any point.

**Architecture:** A new packaging step (`build_mcpb.py`, run only by whoever manages releases —
never by staff) assembles a clean staging directory (reusing `release.py`'s own exclusion list, so
secrets/local data/venv can never leak into the package), writes a `manifest.json` using the
`.mcpb` spec's `uv` server mode (which installs Python + dependencies fresh on the target machine,
sidestepping the compiled-dependency problems the format's docs warn about for Python), bakes in
the organization-wide credentials, and packs it with the official `mcpb` CLI. A new, small
OCR-auto-install step runs once at server startup (before anything else imports the OCR-touching
code) to silently install Tesseract and Poppler if they're missing, closing the one gap `.mcpb`
itself has no story for.

**Tech Stack:** Python 3.11+ (project's existing stack, unchanged), `mcpb` CLI (Node.js tool, run
only on the packaging machine, never by staff), `uv` (managed automatically by Claude Desktop on
the install target — nothing to install manually).

## IMPORTANT — Where This Plan Runs

**This entire plan must be executed on a Windows machine, in a Claude Code session there — not on
a Mac or any other platform.** The project's staff machines are confirmed all-Windows, and the
actual "double-click a `.mcpb` file into Claude Desktop" experience can only be genuinely verified
on that real target platform. If you are an agent picking this plan up: check `sys.platform` or
`%OS%` first, and if you are not on Windows, STOP and report that back — do not attempt to fake or
skip the real-machine verification steps.

Additionally: **building** the `.mcpb` package (Task 1) requires Node.js installed on the machine
running the build (to get the `mcpb` CLI via `npm install -g @anthropic-ai/mcpb`) — this is a
one-time setup step for whoever manages releases, completely separate from what staff members ever
need to install. If Node.js/npm isn't already present, install it first (nodejs.org's Windows
installer, or `winget install OpenJS.NodeJS`) before Task 1.

## Global Constraints

- **This codebase has no pytest and no test framework** (confirmed in `CLAUDE.md`). Every
  verification step in this plan is a plain runnable script or a real, manual end-to-end check —
  not a pytest suite. Do not introduce one.
- **Note on the repo's current layout, since this plan's file paths depend on it:** the project
  root was recently reorganized — most standalone utility scripts (`setup_wizard.py`,
  `apply_update.py`, `release.py`, and the double-click launcher `.bat`/`.command` files) now live
  in `scripts/`, and `safe_io.py` now lives in `core/` (imported elsewhere as `from core import
  safe_io`). Only actual entry points and core config (`main.py`, `mcp_server.py`, `config.py`,
  `requirements.txt`, and the package directories like `ingestion/`/`pipeline/`/`analysis/`) remain
  at the project root. This plan's file paths below already account for this — `build_mcpb.py`
  goes in `scripts/` alongside `release.py` (the sibling script it borrows an exclusion list from);
  `manifest.template.json`, `pyproject.toml`, and `icon.png` stay at the project root alongside
  `requirements.txt`, which plays the same role for the older, non-`.mcpb` install path.
- **Never pack the live project root directly.** `mcpb pack`'s own documented exclusions are only
  `.git, node_modules/.cache, .DS_Store` — nothing about `confidentials/`, `data/`, or `venv/`
  (confirmed by reading the actual mcpb README; there is no `.mcpbignore`/`.gitignore`-style
  exclusion mechanism documented). Every packaging step in this plan assembles a clean, separate
  staging directory first, reusing `scripts/release.py`'s own `EXCLUDED_DIR_NAMES` set
  (`{".git", "venv", ".venv", "env", "ENV", "confidentials", "data", "__pycache__",
  ".pytest_cache", ".mypy_cache"}`), and packs *that* — never the live repo. Since `"data"` (not
  `"data/chroma_db"` specifically) is in that exclusion set, this build script is already immune to
  the exact incident this project once hit where a stray, oddly-named backup folder under `data/`
  slipped past a narrower `.gitignore` pattern and got committed — anything anywhere under `data/`
  is excluded from the staging directory wholesale, regardless of subfolder name.
- **Organization-wide credentials (`OUTLOOK_CLIENT_ID`, `OUTLOOK_TENANT_ID`, `ANTHROPIC_API_KEY`,
  `GOOGLE_PLACES_API_KEY`, `GOOGLE_MAPS_API_KEY`) are baked into the built manifest at package-build
  time, read from the local `confidentials/.env` on the machine doing the build** — never exposed
  as a `user_config` prompt to staff, matching `confidentials/.env.template`'s existing "fill in
  once, distribute" model. The manifest **template** committed to the repo must contain placeholder
  values only, never real secrets — only the *built* `.mcpb` file (never committed to git) contains
  real values.
- **Entry point stays `main.py mcp`** — the exact same invocation already used today
  (`sys.argv[1] == "mcp"` is what `main.py` itself checks to route stdout-safe logging; do not
  change this check or the invocation shape).
- **The OCR auto-install step must run before `ingestion.extractor` is ever imported in the
  process**, not after. `ingestion/extractor.py` does `from config import TESSERACT_PATH,
  POPPLER_PATH` at its own module top level (binds a value once, at first import) and immediately
  sets `pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH` right there — mutating
  `config.TESSERACT_PATH` *after* that module has already been imported would NOT update
  `extractor.py`'s already-bound name or the already-set `pytesseract` global. The only clean fix is
  ordering: run the OCR check/install synchronously, as the very first statement inside
  `run_mcp_server()`, before the watcher/scheduler threads start (those threads are what eventually
  import `ingestion.extractor`, lazily, inside their own thread functions — nothing imports it
  before that point today). This means the auto-install runs once, blocking, adding a one-time
  delay (a real download + silent install, likely 10-30 seconds) only on the very first launch
  after installing this extension on a machine that doesn't already have Tesseract/Poppler; every
  later launch, the check finds them already present and returns near-instantly.

---

### Task 1: Build the packaging script, manifest template, and pyproject.toml

**Files:**
- Create: `manifest.template.json` (project root, alongside `requirements.txt`) — committed,
  placeholder credential values only.
- Create: `pyproject.toml` (project root, alongside `requirements.txt`) — committed, real
  dependency list (mirrors `requirements.txt`, translated to `pyproject.toml` syntax).
- Create: `icon.png` (project root) — a simple 512×512 placeholder icon (can be a plain solid-color
  PNG with a "V" or similar; polish later, not blocking).
- Create: `scripts/build_mcpb.py` — the packaging script, run only by whoever manages releases,
  placed alongside its sibling `scripts/release.py` (the reorganized project's existing convention
  for standalone release-management utilities — this is a new one, not an entry point).

**Interfaces:**
- Consumes: `scripts/release.py`'s `EXCLUDED_DIR_NAMES` set (import it directly, as a sibling
  module in the same `scripts/` directory — don't duplicate the list, since these two exclusion
  lists must never drift apart).
- Produces: a `.mcpb` file at `dist/vaulter-ai-<version>.mcpb` (gitignored — never commit built
  packages).

- [ ] **Step 1: Write `pyproject.toml`**

This mirrors the current `requirements.txt` (verified against its actual content) using
`pyproject.toml`'s dependency syntax, which is what `.mcpb`'s `uv` server mode expects:

```toml
[project]
name = "vaulter-ai"
version = "1.0.0"
description = "Vaulter AI Property Intelligence System"
requires-python = ">=3.11"
dependencies = [
    "pdfplumber",
    "pytesseract",
    "pdf2image",
    "chromadb>=0.5.20",
    "numpy",
    "watchdog",
    "filelock",
    "openpyxl",
    "xlrd",
    "pandas",
    "rapidfuzz",
    "beautifulsoup4",
    "requests",
    "msal",
    "apscheduler",
    "mammoth",
    "python-pptx",
    "python-dotenv",
    "Pillow",
    "anthropic",
    "mcp[cli]",
    "uvicorn",
    "openai",
]
```

- [ ] **Step 2: Write `manifest.template.json`**

Based on the real, verified `hello-world-uv` example structure, adapted for this project. Note the
`args` list runs `main.py mcp` — the exact existing entry point, unchanged:

```json
{
  "manifest_version": "0.4",
  "name": "vaulter-ai",
  "display_name": "Vaulter AI Property Intelligence",
  "version": "1.0.0",
  "description": "Vaulter AI's property intelligence database, connected directly to your own Claude Desktop.",
  "author": {
    "name": "Vaulter LLC"
  },
  "icon": "icon.png",
  "server": {
    "type": "uv",
    "entry_point": "main.py",
    "mcp_config": {
      "command": "uv",
      "args": ["run", "--directory", "${__dirname}", "main.py", "mcp"],
      "env": {
        "OUTLOOK_CLIENT_ID": "__PLACEHOLDER_OUTLOOK_CLIENT_ID__",
        "OUTLOOK_TENANT_ID": "__PLACEHOLDER_OUTLOOK_TENANT_ID__",
        "ANTHROPIC_API_KEY": "__PLACEHOLDER_ANTHROPIC_API_KEY__",
        "GOOGLE_PLACES_API_KEY": "__PLACEHOLDER_GOOGLE_PLACES_API_KEY__",
        "GOOGLE_MAPS_API_KEY": "__PLACEHOLDER_GOOGLE_MAPS_API_KEY__"
      }
    }
  },
  "compatibility": {
    "platforms": ["win32"],
    "runtimes": {
      "python": ">=3.11"
    }
  },
  "keywords": ["real-estate", "property-intelligence", "internal-tool"],
  "license": "UNLICENSED"
}
```

Note: `config.py` currently reads these 5 values via `os.getenv(...)` from `confidentials/.env`
(loaded by `python-dotenv`). Setting them directly in `mcp_config.env` means Claude Desktop's own
`uv`-launched process will have them as real environment variables before `config.py` even runs —
`os.getenv` picks up real process environment variables the same way regardless of whether they
came from a `.env` file or the launching application, so no change to `config.py` is needed for
this to work.

- [ ] **Step 3: Write `scripts/build_mcpb.py`**

Note the `PROJECT_ROOT`/`sys.path` setup mirrors `scripts/release.py`'s own existing pattern exactly
(that file is one directory level below the project root too, and already establishes this
convention — confirmed by reading its actual current content before writing this):

```python
"""
build_mcpb.py
--------------
Vaulter AI -- builds the one-click Claude Desktop Extension (.mcpb) for
distribution. Run this yourself after merging a reviewed change you want
to ship as a new extension version -- NOT run by staff, same trust model
as release.py (this file's sibling in this same scripts/ folder).

Usage:
    python scripts/build_mcpb.py [--version X.Y.Z]

Requires the mcpb CLI (Node.js tool): npm install -g @anthropic-ai/mcpb
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))       # for `import config`
sys.path.insert(0, str(Path(__file__).parent))  # for `import release`, a sibling in scripts/

from release import EXCLUDED_DIR_NAMES, EXCLUDED_FILE_SUFFIXES, EXCLUDED_FILE_NAMES

DIST_DIR = PROJECT_ROOT / "dist"


def _load_real_credentials() -> dict:
    """Reads the real organization-wide values from the LOCAL confidentials/.env
    on this machine -- the machine doing the build, which already has them
    configured, exactly like release.py trusts the local git checkout."""
    from config import (
        OUTLOOK_CLIENT_ID, OUTLOOK_TENANT_ID, ANTHROPIC_API_KEY,
        GOOGLE_PLACES_API_KEY, GOOGLE_MAPS_API_KEY,
    )
    missing = [name for name, val in [
        ("OUTLOOK_CLIENT_ID", OUTLOOK_CLIENT_ID),
        ("OUTLOOK_TENANT_ID", OUTLOOK_TENANT_ID),
        ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
    ] if not val]
    if missing:
        print(f"ERROR: confidentials/.env is missing required organization-wide values: "
              f"{', '.join(missing)}. Fill these in before building a distributable package.",
              file=sys.stderr)
        sys.exit(1)
    return {
        "OUTLOOK_CLIENT_ID": OUTLOOK_CLIENT_ID,
        "OUTLOOK_TENANT_ID": OUTLOOK_TENANT_ID,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "GOOGLE_PLACES_API_KEY": GOOGLE_PLACES_API_KEY,
        "GOOGLE_MAPS_API_KEY": GOOGLE_MAPS_API_KEY,
    }


def _iter_source_files():
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == "dist":
            continue
        if path.suffix in EXCLUDED_FILE_SUFFIXES or path.name in EXCLUDED_FILE_NAMES:
            continue
        if path.name == "manifest.template.json":
            continue
        yield path, rel


def build(version: str) -> Path:
    staging_dir = DIST_DIR / "staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    count = 0
    for path, rel in _iter_source_files():
        dest = staging_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        count += 1
    print(f"Copied {count} source files into the staging directory (confidentials/, data/, "
          f"venv/, and .git/ excluded).")

    credentials = _load_real_credentials()
    manifest = json.loads((PROJECT_ROOT / "manifest.template.json").read_text())
    manifest["version"] = version
    env = manifest["server"]["mcp_config"]["env"]
    for key in list(env.keys()):
        env[key] = credentials[key]
    (staging_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Wrote manifest.json (version {version}) with real organization-wide credentials.")

    result = subprocess.run(["mcpb", "validate", str(staging_dir)], capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"Manifest validation failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    output_path = DIST_DIR / f"vaulter-ai-{version}.mcpb"
    result = subprocess.run(
        ["mcpb", "pack", str(staging_dir), str(output_path)],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"mcpb pack failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"\nBuilt: {output_path}")
    return output_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="1.0.0")
    args = parser.parse_args()
    build(args.version)
```

- [ ] **Step 4: Add `dist/` to `.gitignore`**

Built `.mcpb` packages (containing real credentials) must never be committed. Add this line to
`.gitignore`:

```
dist/
```

- [ ] **Step 5: Install the `mcpb` CLI and confirm it's available**

```bash
npm install -g @anthropic-ai/mcpb
mcpb --version
```

Expected: prints a version number. If `npm` itself isn't found, install Node.js first
(nodejs.org's Windows installer, or `winget install OpenJS.NodeJS`), then retry.

- [ ] **Step 6: Run the build script and confirm it produces a valid package**

```bash
cd <project root>
python scripts/build_mcpb.py --version 1.0.0
```

Expected output ends with `Built: dist/vaulter-ai-1.0.0.mcpb`. The `mcpb validate` step inside the
script must not report errors — if it does, fix the manifest before proceeding to later tasks.

- [ ] **Step 7: Confirm the built package does NOT contain sensitive files**

This is the most important check in this task — verify the exclusion logic actually worked, don't
just trust it:

```bash
python -c "
import zipfile
with zipfile.ZipFile('dist/vaulter-ai-1.0.0.mcpb') as zf:
    names = zf.namelist()
    bad = [n for n in names if n.startswith('confidentials/') or n.startswith('data/') or n.startswith('venv/') or n.startswith('.git/')]
    print(f'{len(names)} total files in package')
    if bad:
        print('FAIL -- sensitive paths found in package:', bad)
    else:
        print('PASS -- no confidentials/, data/, venv/, or .git/ paths in the package')
"
```

Expected: `PASS -- no confidentials/, data/, venv/, or .git/ paths in the package`. If this fails,
STOP — do not proceed to Task 3's real install test until this is fixed, since installing a
package containing real secrets into any test environment would leak them.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml manifest.template.json icon.png scripts/build_mcpb.py .gitignore
git commit -m "$(cat <<'EOF'
Add .mcpb packaging: manifest template, pyproject.toml, build script

New scripts/build_mcpb.py (run only by whoever manages releases,
mirroring release.py's own trust model) assembles a clean staging
directory -- reusing release.py's own EXCLUDED_DIR_NAMES so confidentials/, data/,
and venv/ can never leak into a built package -- writes manifest.json
with the real organization-wide credentials substituted in from the
local confidentials/.env, and packs it with the official mcpb CLI.

manifest.template.json uses .mcpb's "uv" server mode, which installs
Python and every dependency fresh on the target machine rather than
bundling them -- the mode the format's own docs recommend specifically
because it avoids the compiled-dependency problems (e.g. pydantic,
which the MCP Python SDK itself needs) that make traditional Python
bundling unreliable.

Verified: a built package excludes confidentials/, data/, venv/, and
.git/ entirely (checked by inspecting the actual zip contents, not
assumed from mcpb's own documented -- and confirmed incomplete --
default exclusions).
EOF
)"
```

---

### Task 2: OCR auto-install on first run

**Files:**
- Create: `ocr_installer.py` (project root) — the auto-install logic.
- Modify: `mcp_server.py` — call the check at the very start of `run_mcp_server()`.

**Interfaces:**
- Consumes: `config.TESSERACT_PATH`, `config.POPPLER_PATH` (existing), `config._find_executable`/
  `config._find_poppler_bin_dir` (existing, reused rather than duplicated).
- Produces: `ensure_ocr_tools_installed() -> None` in `ocr_installer.py` — idempotent, safe to call
  on every startup; does real work only the first time tools are missing.

- [ ] **Step 1: Write `ocr_installer.py`**

```python
"""
ocr_installer.py
-----------------
Silently installs Tesseract OCR and Poppler on Windows if they aren't
already present -- closes the one gap the .mcpb packaging format has no
story for (it can install Python + Python packages automatically via
its "uv" server mode, but has no mechanism for non-Python external
binaries like these two).

Only does anything on Windows (sys.platform == "win32") and only if
config.py's own existing auto-detection didn't find either tool
already -- on a machine that already has them (or on Mac, where staff
already install both via Homebrew per the README), this is a fast
no-op. Runs once; a marker file records success so a later restart
doesn't repeat the download+install.

Installs per-user, no admin rights required -- Tesseract via its
official silent-install flags into a per-user AppData location (one
of the exact paths config.py's own TESSERACT_PATH auto-detection
already searches, so it's picked up with zero changes to that
detection logic), Poppler by downloading and unzipping (it isn't an
installer at all, just files) to a per-user location matching what
config.py's POPPLER_PATH detection already searches for.

Must be called SYNCHRONOUSLY, before anything else in the process
imports ingestion.extractor -- see mcp_server.py's own call site and
its comment for why.
"""

import logging
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("vaulter.ocr_installer")

# Pinned to a specific, VERIFIED-working release rather than resolved
# "latest" -- tesseract-ocr/tesseract's own GitHub releases inconsistently
# attach Windows installer assets (confirmed directly: the current
# latest release, 5.5.2, has NO Windows assets at all; only some older
# releases do), so a "fetch latest" approach would be unreliable. This
# URL is confirmed live (verified via a direct request that correctly
# redirected to a real, signed download link). Update this constant
# periodically by checking https://github.com/tesseract-ocr/tesseract/releases
# for a release that actually has a Windows .exe attached.
TESSERACT_INSTALLER_URL = (
    "https://github.com/tesseract-ocr/tesseract/releases/download/5.5.0/"
    "tesseract-ocr-w64-setup-5.5.0.20241111.exe"
)

# Poppler's releases reliably have exactly one asset (a zip) on every
# release, so resolving "latest" via the GitHub API is safe here,
# unlike Tesseract above.
POPPLER_LATEST_RELEASE_API = "https://api.github.com/repos/oschwartz10612/poppler-windows/releases/latest"

MARKER_FILE = Path.home() / "AppData" / "Local" / "Vaulter AI" / "ocr_install_complete.marker"
TESSERACT_INSTALL_DIR = Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR"
POPPLER_INSTALL_PARENT = Path.home() / "Packages"


def _tesseract_present() -> bool:
    return shutil.which("tesseract") is not None or (TESSERACT_INSTALL_DIR / "tesseract.exe").exists()


def _poppler_present() -> bool:
    if shutil.which("pdftoppm"):
        return True
    if POPPLER_INSTALL_PARENT.exists():
        for candidate in POPPLER_INSTALL_PARENT.glob("poppler*"):
            if (candidate / "Library" / "bin" / "pdftoppm.exe").exists():
                return True
    return False


def _install_tesseract() -> bool:
    log.warning("Tesseract OCR not found -- downloading and installing silently (no admin "
                "rights needed, this happens once)...")
    installer_path = Path(os.environ.get("TEMP", ".")) / "tesseract-installer.exe"
    try:
        urllib.request.urlretrieve(TESSERACT_INSTALLER_URL, installer_path)
        result = subprocess.run(
            [str(installer_path), "/S", f"/D={TESSERACT_INSTALL_DIR}"],
            timeout=300,
        )
        if result.returncode != 0:
            log.warning(f"Tesseract installer exited with code {result.returncode}")
            return False
        return _tesseract_present()
    except Exception as e:
        log.warning(f"Could not install Tesseract automatically: {e}. Scanned/image-only PDF "
                    f"pages won't be readable until it's installed manually -- see README.md.")
        return False
    finally:
        installer_path.unlink(missing_ok=True)


def _install_poppler() -> bool:
    log.warning("Poppler not found -- downloading and installing silently (no admin rights "
                "needed, this happens once)...")
    try:
        import json
        with urllib.request.urlopen(POPPLER_LATEST_RELEASE_API, timeout=15) as resp:
            release = json.loads(resp.read())
        assets = release.get("assets", [])
        if not assets:
            log.warning("Could not find a Poppler Windows release asset to download.")
            return False
        download_url = assets[0]["browser_download_url"]
        tag = release.get("tag_name", "latest").lstrip("v")

        zip_path = Path(os.environ.get("TEMP", ".")) / "poppler.zip"
        urllib.request.urlretrieve(download_url, zip_path)

        POPPLER_INSTALL_PARENT.mkdir(parents=True, exist_ok=True)
        extract_dir = POPPLER_INSTALL_PARENT / f"poppler-{tag}"
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        zip_path.unlink(missing_ok=True)
        return _poppler_present()
    except Exception as e:
        log.warning(f"Could not install Poppler automatically: {e}. Scanned/image-only PDF "
                    f"pages won't be readable until it's installed manually -- see README.md.")
        return False


def ensure_ocr_tools_installed() -> None:
    """
    Checks for Tesseract/Poppler and installs whichever is missing,
    silently, per-user, no admin rights needed. Safe to call on every
    startup -- a marker file skips the check entirely once both tools
    are confirmed present, so this is a fast no-op after the first
    successful run.
    """
    if sys.platform != "win32":
        return  # Mac staff install both via Homebrew per the README; nothing to automate there.

    if MARKER_FILE.exists():
        return

    tesseract_ok = _tesseract_present() or _install_tesseract()
    poppler_ok = _poppler_present() or _install_poppler()

    if tesseract_ok and poppler_ok:
        MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text("OCR tools confirmed present.")
        log.info("OCR tools (Tesseract + Poppler) are ready.")
    else:
        missing = []
        if not tesseract_ok:
            missing.append("Tesseract")
        if not poppler_ok:
            missing.append("Poppler")
        log.warning(f"Could not automatically install: {', '.join(missing)}. Scanned/image-only "
                    f"PDF pages won't be readable until this is resolved -- see README.md's Setup "
                    f"section for manual install instructions. Everything else works normally.")
```

- [ ] **Step 2: Wire it into `mcp_server.py`**

In `mcp_server.py`'s `run_mcp_server()` function, add the call as the very FIRST statement inside
the function body (before the `_safe_watcher`/`_safe_scheduler` thread-starting code) — this
ordering is required, not optional; see this plan's Global Constraints for why. Show the exact
edit:

```python
def run_mcp_server():
    """
    Start background services then launch the MCP server.
    This is the single command that runs everything. Transport is stdio
    (see this file's header) -- there is no port to configure; a `port`
    parameter existed here previously but did nothing, since stdio has
    no network listener at all.
    """
    # Must run before anything else -- ingestion.extractor (imported
    # lazily by the watcher thread below) binds config.TESSERACT_PATH/
    # POPPLER_PATH at ITS OWN import time and sets pytesseract's global
    # tesseract_cmd right then; installing OCR tools after that module
    # is already imported would not update either already-bound value.
    # This only does real work (a download + silent install) the very
    # first time on a machine missing these tools -- every later start
    # finds a marker file and returns immediately.
    from ocr_installer import ensure_ocr_tools_installed
    ensure_ocr_tools_installed()

    # ── Start background services ─────────────────────────────────
    def _safe_watcher():
        ...
```

(Keep everything else in the function exactly as it already is — only the new import + call are
added, at the very top.)

- [ ] **Step 3: Verify the module compiles and its logic is sound (from any machine)**

```bash
python -m py_compile ocr_installer.py mcp_server.py
```

Expected: no output, exit code 0.

- [ ] **Step 4: Verify the presence-check functions work correctly against a real absent/present state (real test, on the Windows machine)**

```python
python -c "
import ocr_installer
print('tesseract present:', ocr_installer._tesseract_present())
print('poppler present:', ocr_installer._poppler_present())
"
```

Expected: reflects this machine's ACTUAL current state (True/True if already installed from
earlier testing, False for whichever is genuinely absent). This confirms the detection logic runs
without error on real Windows paths before testing the install path itself.

- [ ] **Step 5: If either tool is genuinely absent on this test machine, verify the real silent install works**

This is a real, meaningful test — do not skip it if there's a way to run it safely (e.g. in a
Windows VM or a machine where installing these tools for real is acceptable):

```python
python -c "
import logging
logging.basicConfig(level=logging.INFO)
import ocr_installer
ocr_installer.ensure_ocr_tools_installed()
print('tesseract present after install:', ocr_installer._tesseract_present())
print('poppler present after install:', ocr_installer._poppler_present())
"
```

Expected: both print `True` afterward, and — critically — confirm NO UAC prompt appeared and NO
visible installer window was shown (the whole point of the silent flags). If a UAC prompt DOES
appear, the install directory chosen isn't actually avoiding admin-required paths — investigate
before proceeding; this would defeat the "no technical steps" goal.

- [ ] **Step 6: Confirm `config.py`'s own auto-detection now finds the freshly-installed tools**

```python
python -c "
import importlib
import config
importlib.reload(config)
print('TESSERACT_PATH:', config.TESSERACT_PATH)
print('POPPLER_PATH:', config.POPPLER_PATH)
assert config.TESSERACT_PATH != 'tesseract', 'expected a real path, not the bare-PATH fallback'
assert config.POPPLER_PATH is not None
print('PASS: config.py auto-detection finds the freshly-installed OCR tools')
"
```

Expected: `PASS: config.py auto-detection finds the freshly-installed OCR tools` — this confirms
the install landed in exactly the paths `config.py`'s existing detection already searches, with no
changes needed to that detection code.

- [ ] **Step 7: Confirm a second run does NOT re-download or re-install anything**

This is the marker-file behavior that keeps every later server startup fast — verify it explicitly,
don't just trust the code:

```python
python -c "
import time, logging
logging.basicConfig(level=logging.INFO)
import ocr_installer

assert ocr_installer.MARKER_FILE.exists(), 'expected the marker file from the prior successful install'

start = time.time()
ocr_installer.ensure_ocr_tools_installed()
elapsed = time.time() - start

print(f'second call took {elapsed:.2f}s')
assert elapsed < 2.0, f'expected a near-instant no-op (marker file present), took {elapsed:.2f}s instead -- it may have tried to re-download'
print('PASS: a second call with the marker file present is a fast no-op, no re-download/re-install')
"
```

Expected: `PASS: a second call with the marker file present is a fast no-op, no re-download/re-install`

- [ ] **Step 8: Commit**

```bash
git add ocr_installer.py mcp_server.py
git commit -m "$(cat <<'EOF'
Add silent first-run OCR tool auto-install for the .mcpb package

Closes the one gap .mcpb's own packaging format has no story for --
it can install Python and Python dependencies automatically via "uv"
mode, but has no mechanism for external, non-Python binaries like
Tesseract/Poppler. ensure_ocr_tools_installed() runs once (a marker
file skips it on every later startup) and installs whichever tool is
missing, silently, per-user, no admin rights, into exactly the paths
config.py's existing auto-detection already searches.

Called as the very first statement in run_mcp_server() -- before the
watcher/scheduler threads start -- because ingestion/extractor.py
binds config.TESSERACT_PATH/POPPLER_PATH at its own module import
time and sets pytesseract's global tesseract_cmd right then; running
the install after that module is already imported (which happens
lazily inside the watcher thread) would not update either already-
bound value.

Verified: presence-detection functions work against this machine's
real state, config.py's own detection picks up the freshly-installed
tools with zero changes to that detection code, and (if this machine
was missing either tool) the real silent install completed with no
UAC prompt and no visible installer window.
EOF
)"
```

---

### Task 3: Real end-to-end install test in Claude Desktop

**Files:** No source changes — this task is entirely real-world verification on the actual target
platform.

- [ ] **Step 1: Build the final package**

```bash
python scripts/build_mcpb.py --version 1.0.0
```

Confirm it succeeds and passes the sensitive-file check from Task 1 Step 7 again (re-run that
exact check) before proceeding — never install a package into a real Claude Desktop without that
confirmation.

- [ ] **Step 2: Install it for real**

Double-click `dist/vaulter-ai-1.0.0.mcpb` (or drag it into a running Claude Desktop window).
Confirm:
- The native install dialog appears, showing the extension's name/description.
- Confirming the install does not require a terminal, an admin prompt, or any manual config-file
  editing.
- Installation completes without an error dialog.

- [ ] **Step 3: Confirm the server actually starts and its tools are callable**

Start a new Claude Desktop conversation and ask something that exercises a real tool, e.g. "check
the system health" (which should trigger `check_system_health`) or "list the portfolio." Confirm
you get a real, sensible response — not a connection error.

- [ ] **Step 4: Confirm the background watcher + scheduler threads are genuinely running**

Since this whole architecture depends on background threads staying alive under this NEW launch
method (via `uv run` instead of a direct `python main.py mcp` in the config file), this is the most
important check in this task. Ask Claude to run `check_system_health` and confirm its response
shows `Scheduler: running` (not `still starting up` stuck forever, and not a start error).

- [ ] **Step 5: Confirm OCR auto-install actually happened (or already had tools) correctly**

Check the response from Step 3/4, or look at this machine's own state directly:

```python
python -c "
import ocr_installer
print('tesseract:', ocr_installer._tesseract_present())
print('poppler:', ocr_installer._poppler_present())
"
```

Both should be `True`. If this is a machine that never had these tools before this test, confirm
they were genuinely installed during this real install (not pre-existing) by checking
`ocr_installer.MARKER_FILE`'s timestamp is recent.

- [ ] **Step 6: Confirm a scanned-PDF ingestion actually works end to end**

Drop a genuinely scanned (image-only) PDF into `data/watched_folder/<some state>/<some
property>/` and confirm it gets OCR'd and ingested successfully (check via `get_database_stats` or
by searching for content you know is in that document). This is the real proof the OCR auto-install
didn't just complete technically but actually produces usable functionality.

- [ ] **Step 7: Confirm the organization-wide credentials actually reached the running server**

Ask Claude to check the inbox (`check_inbox_now`) or otherwise trigger something that needs
`OUTLOOK_CLIENT_ID`/`ANTHROPIC_API_KEY` to be correctly set. Confirm it works (or fails only on the
expected "you haven't signed in yet" Outlook auth step — not on a missing/blank client ID error),
proving the baked-in manifest values correctly became real environment variables for the `uv`-launched
process.

- [ ] **Step 8: Document what you found**

Write a short report to `.superpowers/mcpb-test-report.md` (or similar) covering: did each step
above pass, any surprises, any deviation from what this plan expected, and (per the design's own
Section 5) any observations about whether Claude Desktop applied its own update behavior to this
extension that might interact with the project's existing `apply_pending_update` mechanism — note
it, don't attempt to resolve it in this task.

---

### Task 4: Update README with the new one-click path

**Files:**
- Modify: `README.md` — Setup section.

**Interfaces:** None — documentation only.

- [ ] **Step 1: Fix a pre-existing, separate stale-path bug in the README while you're in there**

Before adding the new one-click instructions, fix something the recent `scripts/`/`core/`
reorganization broke: `README.md` currently says (around its "3. Double-click 'Setup Vaulter AI'"
and "5. Double-click 'Sign In to Outlook'" steps) that these files are directly "inside the
unzipped folder" — but the reorg moved all four launcher files into `scripts/` without updating
this text. Confirm the current wording (`grep -n "Setup Vaulter AI\|Sign In to Outlook" README.md`)
and update it to say `scripts/Setup Vaulter AI.command` / `scripts/Setup Vaulter AI.bat` /
`scripts/Sign In to Outlook.command` / `scripts/Sign In to Outlook.bat`, matching where they
actually are now.

- [ ] **Step 2: Add the one-click path as the new primary Setup instructions**

Replace (or place above, as the new recommended path, keeping the double-click-launcher
instructions — now correctly pointing at `scripts/`, per Step 1 — as a documented fallback below
it) the README's current "Setup" section's lead instructions with something to this effect:
download the `.mcpb` file from wherever it's distributed internally, double-click it, confirm the
install in Claude Desktop, then sign into Outlook (the one remaining personal step). Keep the
existing double-click-launcher and manual `pip install` instructions as the documented
fallback/troubleshooting path underneath, exactly as Priority 3's own README changes already did
for the wizard vs. manual steps.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document the one-click .mcpb install as the new primary Setup path

Also fixes a stale reference from the recent scripts/ reorganization:
the launcher .bat/.command files moved into scripts/, but README.md
still said they were directly inside the unzipped folder root.
EOF
)"
```
