"""
apply_update.py
----------------
Vaulter AI -- install a staged update (Priority 4 in
docs/MULTI_USER_TRANSITION.md).

Since 2026-11-19 updates install by themselves on a normal Windows install
(design: docs/superpowers/specs/2026-11-19-auto-update-restart-design.md):

  * `--auto --startup` runs from main.py before the server imports anything,
    and installs a staged update that needs no dependency change while no
    other server from this install is running;
  * `--auto` runs from the scheduled task (scripts/auto_restart.py) after it
    has closed an idle Claude Desktop, and installs anything staged.

Both go through auto_apply() below, which refuses on a development copy,
refuses a package that does not look like a release (no main.py, or a VERSION
that does not match), backs every file up first, and puts the backup back if
the install raises, its dependencies fail to install, or the new code cannot
even be imported (a smoke test in a child process). Nothing is asked of the
person. The apply_pending_update MCP tool and this file's interactive
`python apply_update.py` remain for a machine where the automatic route is
not active (a Mac, a development copy, or one whose restart task could not be
set up): there a human still says yes first.

Installing means:
  1. Extracting the staged code package to a temp folder.
  2. Deleting any file in this project that the new version no longer
     has (so removed files don't linger as stale dead code).
  3. Copying every file from the new version into place (VERSION last).
  4. Re-installing from requirements.txt with the same Python already
     running this project, only when the release changed that file.
  5. Cleaning up the staging area.

confidentials/ and data/ are NEVER touched -- the update package itself
never contains them (see release.py's exclusion list), so this can't
and doesn't delete or overwrite your secrets or local data. The virtual
environment's FILES aren't touched either, though step 4 does install
packages into it, same as any other `pip install`.

The new code takes effect when Claude Desktop restarts: at startup it is
installed before the server loads; by the scheduled task, Desktop is
reopened after the install.
"""

import base64
import logging
import os
import stat
import time as _time
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


# ---------------------------------------------------------------------------
# File operations that survive what Windows does to an install (hardening pass,
# 2026-11-19). Each was reproduced for real in check_auto_update.py section 6:
#   * a read-only file (attribute set) cannot be deleted or overwritten;
#   * antivirus, the search indexer or an Explorer preview holds a file for a
#     moment, so a delete fails once and works a second later;
#   * a path past 260 characters fails outright while Windows' long-path setting
#     is off, which is the default (off on the maintainer's machine too), and the
#     backup folder adds about 33 characters of depth;
#   * a folder held open (Explorer, a console's current folder) cannot be removed.
# ---------------------------------------------------------------------------

_RETRY_DELAYS = (0.2, 0.5, 1.0, 2.0)        # ~3.7 s in all, then the error stands


def _long(p: Path) -> Path:
    """p as an extended-length path on Windows (the \\\\?\\ prefix), which lifts the
    260-character limit for every file call. Unchanged elsewhere."""
    if sys.platform != "win32":
        return Path(p)
    s = str(p)
    if s.startswith("\\\\?\\"):
        return Path(s)
    s = os.path.abspath(s)
    if s.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + s[2:])
    return Path("\\\\?\\" + s)


def _retrying(action, path: Path):
    """Run action(); on PermissionError clear a read-only attribute and try again
    after a short wait, a few times. The last error is raised."""
    for i in range(len(_RETRY_DELAYS) + 1):
        try:
            return action()
        except PermissionError:
            if i == len(_RETRY_DELAYS):
                raise
            try:
                if os.path.lexists(path):
                    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
            _time.sleep(_RETRY_DELAYS[i])


def _unlink(path: Path) -> None:
    path = _long(path)          # a too-long plain path reads as "not found", and missing_ok hides it
    _retrying(lambda: path.unlink(missing_ok=True), path)


def _copy(src: Path, dest: Path) -> None:
    src, dest = _long(src), _long(dest)
    _retrying(lambda: shutil.copy2(src, dest), dest)


def _rmtree(path: Path) -> None:
    """Delete a folder, read-only files included. Never raises: whatever still
    cannot go is left, and callers that need it gone check afterwards."""
    root = _long(path)
    if not root.exists():
        return
    for d, _dirs, files in os.walk(root, topdown=False):
        for f in files:
            try:
                _unlink(Path(d) / f)
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass


def _sync_tree(project_root: Path, src_dir: Path) -> tuple[int, int]:
    """
    Make project_root match src_dir exactly, except for anything under a
    PRESERVED_DIR_NAMES path: delete files src_dir lacks, copy every file
    src_dir has, drop now-empty folders. Returns (copied, deleted). Shared by
    the apply and by the restore of a pre-update backup, so both behave alike.
    Read-only files, brief locks and over-long paths are handled (see above).
    """
    project_root, src_dir = _long(project_root), _long(src_dir)
    new_files = {
        p.relative_to(src_dir) for p in src_dir.rglob("*") if p.is_file()
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
            _unlink(existing)
            deleted += 1

    # Step 2: copy every file from the new version into place.
    updated = 0
    # VERSION goes LAST, so a VERSION that already changed means every other
    # file was copied before it.
    for rel in sorted(new_files, key=lambda r: r == Path("VERSION")):
        src = src_dir / rel
        dest = project_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _copy(src, dest)
        updated += 1

    # Remove any now-empty directories left behind by step 1 (never
    # inside a preserved path, and never the project root itself). A folder
    # that cannot be removed (open in Explorer) is left: an empty folder is
    # harmless, and failing the whole install over it would undo a good update.
    for d in sorted(project_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if d.is_dir() and not _is_preserved(d.relative_to(project_root)) and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    return updated, deleted


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
        return _sync_tree(project_root, tmp_dir)


# The only two places a launcher/agent-file package may write, relative to the
# install root. Anything else in such a package is ignored rather than trusted.
ALLOWED_EXTRA_TOP_LEVEL = ("quick_start", ".claude")

# Never written even when present in a package: the hooks folder holds the
# confidential-name list the leak hook uses, and settings files are per-machine.
# release.py already excludes both; this is the receiving end refusing them
# independently, so a package built by an older or hand-made script cannot
# place them either.
REFUSED_EXTRA_PATHS = (Path(".claude") / "hooks", Path(".claude") / "worktrees")
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


log = logging.getLogger("vaulter.update")


class _Step:
    """
    Time each stage of an apply and write it to the log.

    Added 2026-08-21 because this function was a black box. A real apply
    inside Claude Desktop ran 8 minutes 17 seconds without writing a single
    line, then never returned -- while the work itself had finished. With no
    record of which stage was slow, the cause could only be guessed at, and
    two confident guesses (pip blocking on an inherited pipe, and a slow
    first-time import of the signing library) were both WRONG when finally
    measured: 1.4s and 0.08s.

    The lesson is the same one this project keeps relearning: when something
    cannot be observed, the answer is instrumentation, not another theory.
    """

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = _time.perf_counter()
        log.info(f"[APPLY] {self.name}: starting")
        return self

    def __exit__(self, *exc):
        took = _time.perf_counter() - self.t
        if exc[0] is not None:
            log.warning(f"[APPLY] {self.name}: FAILED after {took:.1f}s "
                        f"({exc[0].__name__}: {exc[1]})")
        else:
            log.info(f"[APPLY] {self.name}: done in {took:.1f}s")
        return False

def refresh_dependencies(project_root: Path, timeout: int = 600) -> tuple[bool, str]:
    """
    Re-installs from requirements.txt using the SAME Python interpreter
    already running this project, so a fix that adds or changes a
    dependency doesn't leave the app broken after its code is updated
    but the new package it needs isn't installed.

    This used to claim pip was "safe and fast to run on every apply". That
    was wrong in production: on 2026-08-21 a real apply inside Claude
    Desktop ran long enough that the tool call timed out and the user was
    told the update had FAILED -- while it had in fact succeeded. So the
    caller now skips this entirely when requirements.txt has not changed,
    which is almost every update, and this function is only reached when
    there is genuinely something to install.

    `timeout` is 600 s for the manual path; the automatic install passes less
    (AUTO_PIP_TIMEOUT), so the scheduled task's own time limit is never reached
    while Claude Desktop is closed.

    Returns (ok, message) -- message is empty on success, or pip's own
    error output (truncated) on failure. Never raises.
    """
    requirements = project_root / "requirements.txt"
    if not requirements.exists():
        return True, ""

    # pip writes to a FILE, not to pipes.
    #
    # This is the whole bug. capture_output=True hands pip two pipes, and the
    # parent then has to drain them -- subprocess.run does that inside
    # communicate(), using reader threads. Inside this server's event loop
    # those threads crawl. Traced through the real transport on 2026-08-21:
    # the same pip call took 3 MINUTES 21 SECONDS here against ~2 seconds
    # from a terminal, which is what made an update look like it had failed
    # while it was quietly still working.
    #
    # This codebase already knew this shape: _get_code_version runs git on a
    # background thread with its own queue timeout for exactly the same
    # reason, measured 2026-07-30. A file needs no reader thread at all, so
    # there is nothing to starve, and pip's output is still kept for the
    # failure message.
    import tempfile as _tf
    out_path = Path(_tf.gettempdir()) / "vaulter_pip_output.txt"
    try:
        with open(out_path, "w+", encoding="utf-8", errors="replace") as fh:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
                stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, timeout=timeout,
            )
        output = out_path.read_text(encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False, f"pip timed out after {timeout} s"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
    if result.returncode != 0:
        return False, output.strip()[-2000:]
    return True, ""


def apply_pending_update(project_root: Path = None, *, pip_timeout: int = 600) -> dict:
    """
    Applies whatever update is currently staged, with NO interactive
    prompt. The manual callers (this file's own main(), or the
    apply_pending_update MCP tool in mcp_server.py) get a human's explicit
    go-ahead BEFORE calling this; auto_apply() calls it only after its own
    guards and wraps it in a backup. This function just does the work once
    called. Never raises for the
    ordinary "nothing staged" / "staged file missing" cases -- always
    returns a result dict describing what happened. `pip_timeout` bounds the
    dependency install (see refresh_dependencies).
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
            "signature_failed": True,
            "reason": f"the staged package for version {version} failed signature "
                      f"verification at apply time -- refusing to apply it. Delete "
                      f"{zip_path} and the pending update record, then let it re-download.",
        }

    # Leave a note saying an apply has begun. Deleted on success, so if it is
    # still here next time something started and never finished -- the only
    # trace a hang leaves, since a hang logs nothing at all.
    try:
        from config import APPLY_IN_PROGRESS_FILE
        Path(APPLY_IN_PROGRESS_FILE).write_text(json.dumps({
            "version": version,
            "signature": pending.get("signature") or "",
            "started": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except Exception:
        pass                  # never let bookkeeping stop a real update

    # What the dependency list looks like BEFORE the sync overwrites it, so we
    # can tell afterwards whether this release actually changed it.
    _req = project_root / "requirements.txt"
    try:
        deps_before = _req.read_bytes() if _req.exists() else b""
    except OSError:
        deps_before = None          # cannot tell -- so do not skip

    with _Step(f"syncing {zip_path.name} into place"):
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

    # Only touch pip when this release actually changed the dependency list.
    #
    # It used to run on every apply, on the reasoning that pip skips
    # already-satisfied packages quickly. In production it did not: on
    # 2026-08-21 a real apply inside Claude Desktop ran long enough that the
    # tool call timed out, and the user was told the update had FAILED while
    # it had actually SUCCEEDED -- the worst of both, because it invites
    # someone to go fixing a machine that is already fine.
    #
    # Almost no release changes requirements.txt, so almost every apply now
    # does no pip work at all and finishes in about a second. When a release
    # DOES change it, pip runs exactly as before -- which is the case the
    # step exists for.
    #
    # If the old contents could not be read, that is 'cannot tell', and it
    # runs pip. Never skip on a maybe.
    try:
        deps_after = _req.read_bytes() if _req.exists() else b""
    except OSError:
        deps_after = None
    if deps_before is not None and deps_after is not None and deps_before == deps_after:
        deps_ok, deps_message = True, ""
        deps_skipped = True
    else:
        with _Step("refreshing dependencies (requirements.txt changed)"):
            deps_ok, deps_message = refresh_dependencies(project_root, timeout=pip_timeout)
        deps_skipped = False

    zip_path.unlink(missing_ok=True)
    (PENDING_UPDATE_DIR / "ready.json").unlink(missing_ok=True)

    # Forget when this machine last reported to the team, so the next
    # conversation reports the NEW version straight away instead of leaving
    # the team list claiming the old one until tomorrow. This belongs HERE,
    # not in the MCP tool that usually calls it: it used to live there, so
    # applying an update any other way -- including this file's own
    # command-line fallback -- left the list stating a version the machine
    # was no longer running. Measured 2026-08-20 on the maintainer's own
    # install, which reported a version it had not been on for an hour.
    # "Did that fix reach everyone?" is the one question that list answers.
    try:
        from config import CHECKIN_STAMP_FILE
        Path(CHECKIN_STAMP_FILE).unlink(missing_ok=True)
    except Exception:
        pass                  # never let bookkeeping fail a real update

    log.info("[APPLY] all stages complete -- returning success")
    try:
        from config import APPLY_IN_PROGRESS_FILE
        Path(APPLY_IN_PROGRESS_FILE).unlink(missing_ok=True)
    except Exception:
        pass
    return {
        "applied": True,
        "version": version,
        "files_updated": updated,
        "files_deleted": deleted,
        "extras_written": extras_written,
        "extras_note": extras_note,
        "dependencies_ok": deps_ok,
        "dependencies_skipped": deps_skipped,
        "dependencies_message": deps_message,
        "dependencies_timed_out": (not deps_ok) and str(deps_message).startswith("pip timed out"),
    }


# ---------------------------------------------------------------------------
# Automatic install (2026-11-19)
#
# Everything below derives its paths from project_root (the `system` folder)
# using config's own layout, NOT from config's constants, so it can be pointed
# at a scratch tree. apply_pending_update() above still reads config itself.
# ---------------------------------------------------------------------------

# Above the automatic pip limit plus backup, sync and smoke test, so a lock held
# by a live install is never mistaken for one left by a dead process.
LOCK_STALE_SECONDS = 900

# How long each step of an automatic install may take. The scheduled task has a
# 60-minute limit, and the idle path's worst case (pip 300 s + smoke test 120 s +
# the channel check and the 30 s exit wait) stays well inside it. At startup the
# whole installer runs inside main.py's own 120 s limit, so its steps are bounded
# tighter: a step killed from outside cannot undo itself.
AUTO_PIP_TIMEOUT = 300
SMOKE_TEST_TIMEOUT = 120
STARTUP_PIP_TIMEOUT = 60
STARTUP_SMOKE_TEST_TIMEOUT = 45
STARTUP_LOCK_WAIT_SECONDS = 60
# main.py gives the startup installer 120 s and then kills it. Everything the
# installer starts must be able to finish inside this budget (counted from the
# installer's own start, lock wait included), with room to spare.
STARTUP_BUDGET_SECONDS = 100
STARTUP_MARGIN_SECONDS = 20          # the backup and file copy, which have no timeout of their own
STARTUP_MIN_PIP_SECONDS = 30         # less than this is not worth starting pip for
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_development_copy(project_root: Path) -> bool:
    """A git checkout (`.git` file or folder beside system/) or a tree with no
    VERSION file is somebody's working copy; an automatic install would
    overwrite their uncommitted work."""
    return (project_root.parent / ".git").exists() or not (project_root / "VERSION").exists()


def running_version(project_root: Path) -> str:
    try:
        text = (project_root / "VERSION").read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = text.splitlines()
    return lines[0].strip() if lines else ""


def staged_update_applicable(pending, running: str, data_dir: Path | None = None) -> str:
    """The version worth installing, or "". Not a dict, no version, the version
    already running, or a stage made while a different version was running (the
    machine has since moved on, so installing it could be a downgrade). Given
    data_dir, also "" for a version set aside after failing to install (see
    version_set_aside): the one place this rule lives, so the installer, the
    restart task and the chat notice all ask the same question."""
    if not isinstance(pending, dict):
        return ""
    version = pending.get("version")
    if not version or not isinstance(version, (str, int, float)) or version == running:
        return ""
    at_download = pending.get("current_version_at_download")
    if at_download and running and at_download != running:
        return ""
    if data_dir is not None and version_set_aside(data_dir, str(version), pending.get("signature")):
        return ""
    return str(version)


# ---------------------------------------------------------------------------
# Versions that keep failing (hardening pass). Without this, a release whose
# dependencies cannot install (no internet), or whose code cannot start, was put
# back and then tried again at every start and every idle restart, for ever,
# with an [ERROR] each time. Two failed installs of one version set it aside
# until a different version is published (or the same version is re-published
# with a different package, i.e. a new signature).
# ---------------------------------------------------------------------------

FAILURE_LIMIT = 2


def _failures_path(data_dir: Path) -> Path:
    return Path(data_dir) / "update_failures.json"


def _read_failures(data_dir: Path) -> dict:
    try:
        data = json.loads(_failures_path(data_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def version_set_aside(data_dir: Path, version: str, signature=None) -> bool:
    """True if version failed FAILURE_LIMIT times here and the package has not
    changed since (a different signature means a re-published package: try it)."""
    entry = _read_failures(data_dir).get(str(version))
    if not isinstance(entry, dict) or entry.get("set_aside") is not True:
        return False
    recorded = entry.get("signature")
    return not (signature and recorded and recorded != signature)


def record_failure(data_dir: Path, version: str, signature=None) -> bool:
    """Count one failed automatic install of version. True when this failure is
    the one that sets it aside. Never raises (a record that cannot be written
    means the version is simply tried again)."""
    try:
        data = _read_failures(data_dir)
        entry = data.get(str(version))
        entry = entry if isinstance(entry, dict) else {}
        if signature and entry.get("signature") and entry.get("signature") != signature:
            entry = {}
        try:
            n = max(0, int(entry.get("failures", 0)))
        except (TypeError, ValueError):
            n = 0
        n += 1
        was = entry.get("set_aside") is True
        data[str(version)] = {"failures": n, "set_aside": n >= FAILURE_LIMIT,
                              "signature": str(signature or "")}
        path = _failures_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return n >= FAILURE_LIMIT and not was
    except Exception:
        return False


def discard_stage(data_dir: Path, pending) -> None:
    """Remove the staged record and its package(s), so the next channel check
    downloads a fresh copy (or nothing, for a set-aside version). Never raises."""
    pend = Path(data_dir) / "pending_update"
    # Only if ready.json still names the stage that was judged (review M-7):
    # another process may have staged a different version since it was read.
    names = []
    try:
        now = json.loads((pend / "ready.json").read_text(encoding="utf-8"))
    except Exception:
        now = None                          # gone or unreadable: nothing else of ours to keep
    same = not isinstance(now, dict) or not isinstance(pending, dict) or (
        str(now.get("version")) == str(pending.get("version"))
        and (not pending.get("zip_filename") or now.get("zip_filename") == pending.get("zip_filename")))
    if same:
        names.append("ready.json")
    if isinstance(pending, dict):
        keep = {str(now.get(k) or "") for k in ("zip_filename", "extras_zip_filename")} \
            if isinstance(now, dict) and not same else set()
        names += [n for n in (str(pending.get(k) or "") for k in ("zip_filename", "extras_zip_filename"))
                  if n not in keep]
    for name in names:
        if not name or Path(name).name != name:
            continue                       # never follow a path out of the folder
        try:
            _unlink(pend / name)
        except OSError:
            pass


def stage_problem(data_dir: Path, pending, version: str) -> str:
    """Why the staged package for version cannot be installed as it stands
    (missing, unreadable, or not shaped like a release), or "". Cheap: no
    signature check, which apply_pending_update does right before writing."""
    name = str(pending.get("zip_filename") or "") if isinstance(pending, dict) else ""
    zip_path = Path(data_dir) / "pending_update" / name
    if not name or not zip_path.is_file():
        return "missing"
    return staged_package_problem(zip_path, version)


def set_aside_after_failure(data_dir: Path, pending, version: str, what: str) -> bool:
    """Count a failed install of version; on the failure that sets it aside, log
    the one [ERROR] saying so and drop its stage. True if it is now set aside."""
    signature = pending.get("signature") if isinstance(pending, dict) else None
    if record_failure(data_dir, version, signature):
        log.error(f"automatic update to {version} has failed {FAILURE_LIMIT} times ({what}); "
                  f"it is set aside and will not be tried again until a newer version is "
                  f"published. This computer stays on the version it has.")
        discard_stage(data_dir, pending)
        return True
    return version_set_aside(data_dir, version, signature)


def release_changes_dependencies(project_root: Path, zip_path: Path) -> bool:
    """True if the package's requirements.txt differs from the installed one.
    Cannot tell (either unreadable) counts as changed: never assume safe."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            new = zf.read("requirements.txt")
        old = (project_root / "requirements.txt").read_bytes()
    except Exception:
        return True
    return new != old


def _norm_cmd(text: str) -> str:
    return text.replace("/", "\\").lower()


def _count_servers(procs: list, main_py: Path, self_pid: int) -> int:
    """
    Servers other than this one running `main.py mcp` for this install.
    procs is (pid, parent_pid, command_line). On Windows a venv's python.exe is
    a launcher that starts the real interpreter as a child with the same command
    line, so every server is two matching processes. Only ROOT matches (parent
    not itself a match) count, and the root of this process's own chain is left
    out -- otherwise the check would always see itself and never allow an install.
    """
    target = _norm_cmd(str(main_py))
    matching = {}
    for pid, ppid, cmd in procs:
        norm = _norm_cmd(cmd or "")
        if target in norm and norm.rstrip().endswith(" mcp"):
            matching[pid] = ppid
    roots = {pid for pid, ppid in matching.items() if ppid not in matching}
    # Our own server is the first matching ROOT among our ancestors. Walk through
    # non-matching ones too: the `--auto --startup` CLI is a child of its server.
    parent_of = {pid: ppid for pid, ppid, _ in procs}
    cur = self_pid
    for _ in range(64):
        if cur in matching and matching[cur] not in matching:
            roots.discard(cur)
            break
        cur = parent_of.get(cur)
        if cur is None:
            break
    return len(roots)


def powershell_exe() -> str:
    """Windows PowerShell by its full path, so a PATH that lost System32's
    PowerShell folder (it happens: an installer rewriting PATH) does not quietly
    turn every process listing and task query into "could not tell"."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    p = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.is_file() else "powershell"


def other_servers_running(project_root: Path) -> bool:
    """True if another MCP server from this install is running. Windows only;
    False on any failure (the caller then proceeds -- a missed server costs one
    update applied under it, which the backup covers). Output goes to a file,
    never a pipe, for the reason refresh_dependencies gives, and in UTF-8: with
    the console's code page an install path holding a letter like 'é' came back
    mangled and never matched, so a running server went unseen."""
    if sys.platform != "win32":
        return False
    out_path = Path(tempfile.gettempdir()) / f"vaulter_procs_{os.getpid()}.json"
    try:
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                  "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json -Compress")
        with open(out_path, "w", encoding="utf-8") as fh:
            result = subprocess.run(
                [powershell_exe(), "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=fh, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=20,
                creationflags=_NO_WINDOW,
            )
        if result.returncode != 0:
            return False
        raw = out_path.read_text(encoding="utf-8-sig", errors="replace").strip()
        data = json.loads(raw) if raw else []
        if isinstance(data, dict):
            data = [data]
        procs = [(int(d["ProcessId"]), int(d.get("ParentProcessId") or 0),
                  d.get("CommandLine") or "") for d in data]
        return _count_servers(procs, project_root / "main.py", os.getpid()) > 0
    except Exception:
        return False
    finally:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass


def _backup_dir(project_root: Path, version: str) -> Path:
    return project_root / "data" / "backups" / f"pre_update_{version}"


def _marker_path(project_root: Path, version: str) -> Path:
    # A sibling of the backup folder, NOT inside it: the folder is synced back
    # into the tree on restore, so a marker inside would be copied into it.
    return project_root / "data" / "backups" / f"pre_update_{version}.complete"


def _backup_tree(project_root: Path, dest: Path, marker: Path, source_version: str) -> None:
    """Copy every non-preserved file to dest, then write marker LAST. A backup
    without its marker is partial and must never be restored (a restore deletes
    whatever the backup lacks)."""
    _unlink(marker)
    project_root, dest = _long(project_root), _long(dest)
    _rmtree(dest)
    if dest.exists() and any(p.is_file() for p in dest.rglob("*")):
        raise OSError("an earlier backup folder could not be cleared")
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(project_root)
        if _is_preserved(rel):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy(p, target)
        count += 1
    _long(marker).write_text(json.dumps({"source_version": source_version, "files": count}),
                      encoding="utf-8")


def _discard_backup(dest: Path, marker: Path) -> None:
    _rmtree(dest)
    try:
        _unlink(marker)
    except OSError:
        pass


def _backup_is_usable(dest: Path, marker: Path, current_version: str, target_version: str) -> bool:
    """Complete (marker present, file count matches) and the tree is either still
    on the version the backup was made from, or already on the update's target
    (a half-applied tree has copied the new VERSION). Any OTHER running version
    means the machine moved on, and a backup of an older tree must not go over it."""
    try:
        info = json.loads(_long(marker).read_text(encoding="utf-8"))
        dest = _long(dest)
        if (not isinstance(info, dict) or not dest.is_dir()
                or current_version not in (info.get("source_version"), target_version)):
            return False
        return sum(1 for p in dest.rglob("*") if p.is_file()) == info.get("files")
    except Exception:
        return False


def staged_package_problem(zip_path: Path, version: str) -> str:
    """Why this staged package does not look like a Vaulter AI release, or "".
    A cheap guard, not a signature: the marker naming the version is not signed,
    so an automatic install at least insists the zip holds main.py at its top
    level and a VERSION whose first line is the version it is installed as."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if "main.py" not in names:
                return "it has no main.py at its top level"
            if "VERSION" not in names:
                return "it has no VERSION file"
            lines = zf.read("VERSION").decode("utf-8", errors="replace").splitlines()
    except Exception:
        return "it cannot be opened"
    if not lines or lines[0].strip() != str(version):
        return "its VERSION file does not match the version it is staged as"
    return ""


# The smoke test's child program. mcp_server's own top level imports only the
# standard library, so importing it alone proved little; config (every entry
# point's first import) and the MCP library (a dependency that pip may just have
# changed) are imported too. config creates folders when it is imported,
# including in the team's shared folder; a release being TESTED must not do that
# (if it fails, it is put back, and the folders would stay behind in everyone's
# folder), so for this child only, creating a folder outside the install does
# nothing. Nothing else in config writes at import time (checked 2026-11-19).
_SMOKE_CODE = """\
import pathlib, sys
root = pathlib.Path({root!r}).resolve()
sys.path.insert(0, str(root))
_mkdir = pathlib.Path.mkdir
def _mkdir_inside_only(self, *a, **k):
    try:
        inside = pathlib.Path(self).resolve().is_relative_to(root)
    except Exception:
        inside = False
    if inside:
        return _mkdir(self, *a, **k)
pathlib.Path.mkdir = _mkdir_inside_only
import config
import mcp.server.fastmcp
import mcp_server
"""


def _smoke_test(project_root: Path, timeout: int = SMOKE_TEST_TIMEOUT) -> bool:
    """Can the freshly installed code be imported at all? config, the MCP library
    and mcp_server, in a child process (stdin closed, output to a temp file,
    never pipes); see _SMOKE_CODE. False on a non-zero exit, a timeout, or
    anything else going wrong. Checks replace this function to force a pass or a
    failure without importing anything."""
    out = Path(tempfile.gettempdir()) / f"vaulter_smoke_{os.getpid()}_{_time.time_ns()}.txt"
    code = _SMOKE_CODE.format(root=str(project_root))
    try:
        with open(out, "w", encoding="utf-8", errors="replace") as fh:
            r = subprocess.run([sys.executable, "-c", code], cwd=str(project_root),
                               stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                               timeout=timeout, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass


def _in_progress_path(project_root: Path) -> Path:
    return project_root / "data" / "apply_in_progress.json"


def _write_in_progress(project_root: Path, version: str, signature: str = "",
                       counted: bool = False) -> None:
    """The note recover_unfinished_apply looks for. Written around the steps of
    an automatic install that run AFTER apply_pending_update has cleared its own
    note (the smoke test, a restore), so a process killed there is still
    recovered at the next start. `counted`: this failure is already in
    update_failures.json (a restore), so the recovery must not count it twice."""
    try:
        _in_progress_path(project_root).write_text(json.dumps({
            "version": version,
            "signature": signature or "",
            "counted": bool(counted),
            "started": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except Exception:
        pass


def _clear_in_progress(project_root: Path) -> None:
    try:
        _in_progress_path(project_root).unlink(missing_ok=True)
    except OSError:
        pass


def _restore(project_root: Path, backup: Path, marker: Path, version: str) -> bool:
    """Put the pre-update backup back. True on success (backup and note then
    removed). On failure the backup and the in-progress note stay, so the next
    start's recovery tries again."""
    _write_in_progress(project_root, version, counted=True)
    try:
        _sync_tree(project_root, backup)
    except Exception as e:
        log.error(f"could not put the previous version back after the update to {version} "
                  f"failed ({type(e).__name__}); it will be tried again at the next start")
        return False
    _discard_backup(backup, marker)
    _clear_in_progress(project_root)
    return True


def _startup_record_path(project_root: Path) -> Path:
    return project_root / "data" / "startup_installed.json"


def _record_startup_install(project_root: Path, version: str) -> None:
    """Tell the restart task this version was installed at startup, i.e. BEFORE
    the server loaded it: its VERSION file is newer than Desktop's start, but no
    restart is owed for it."""
    try:
        _startup_record_path(project_root).write_text(json.dumps({"version": version}),
                                                      encoding="utf-8")
    except Exception:
        pass


def _restart_task_gave_up(project_root: Path, version: str) -> bool:
    """True if the restart task's state says it stopped restarting for version."""
    try:
        state = json.loads((project_root / "data" / "auto_restart.json").read_text(encoding="utf-8"))
        entry = state.get(version) if isinstance(state, dict) else None
        return isinstance(entry, dict) and bool(entry.get("gave_up"))
    except Exception:
        return False


def _pid_alive(pid: int):
    """True / False if the process is / is not running, None when it cannot be
    told. Never os.kill(pid, 0) on Windows: there it sends Ctrl+C."""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            err = ctypes.get_last_error()
            return False if err == 87 else (True if err == 5 else None)   # no such pid / access denied
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return None
            return code.value == 259                                    # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return None


def _lock_is_stale(lock: Path) -> bool:
    """A lock nobody holds any more: older than LOCK_STALE_SECONDS; dated more than
    a minute in the FUTURE (written before the clock was put back, it would
    otherwise look fresh for as long as the clock was off -- and every start
    would wait for it); or naming a process that is no longer running (an
    installer killed by main.py's 120 s limit leaves its lock behind)."""
    try:
        age = _time.time() - lock.stat().st_mtime
    except OSError:
        return False
    if age > LOCK_STALE_SECONDS or age < -60:
        return True
    try:
        pid = int(lock.read_text(encoding="utf-8", errors="replace").strip() or "0")
    except (OSError, ValueError):
        return False
    return pid > 0 and pid != os.getpid() and _pid_alive(pid) is False


def _lock_is_fresh(lock: Path) -> bool:
    return lock.exists() and not _lock_is_stale(lock)


def _wait_for_lock(lock: Path, seconds: float = STARTUP_LOCK_WAIT_SECONDS, *,
                   sleep=_time.sleep, clock=_time.monotonic) -> bool:
    """Wait (wall time) for a fresh lock to clear. True when none is held, False
    if one still is after `seconds`. A stale lock counts as not held."""
    deadline = clock() + seconds
    while _lock_is_fresh(lock):
        if clock() >= deadline:
            return False
        sleep(1)
    return True


def _take_lock(lock: Path) -> bool:
    import os
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not lock.exists():
                continue
            if _lock_is_stale(lock):
                try:
                    lock.unlink()
                except OSError:
                    return False
                continue
            return False
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def auto_apply(project_root: Path | None = None, *, at_startup: bool = False,
               deadline: float | None = None, clock=_time.monotonic) -> dict:
    """
    Install the staged update without asking, but only when that is safe.
    Never raises. Returns {"applied", "version", "reason", "restored"}.

    Refused: a development copy; nothing applicable staged; a package without
    main.py at its top level or whose VERSION does not match ([ERROR]).
    At startup (the server is about to start serving) an update that changes
    dependencies, or one landing while another server from this install runs,
    is left for the idle restart: pip must not run under a live server. The one
    exception: a version the restart task gave up on may change dependencies
    here, since nothing else will install it.

    Every file is backed up first and put back ([ERROR], restored=True) if the
    install raises, its dependencies fail to install, or the new code cannot be
    imported in a child process.

    Hardening pass: a staged package that is missing, unreadable, not shaped
    like a release, or fails its signature is thrown away so the next channel
    check downloads it again; each failed install of a version is counted, and
    the second one sets that version aside until a newer one is published
    (set_aside_after_failure), so nothing retries for ever. `deadline` (a
    `clock()` value) is the startup budget: steps that could not finish before
    it are left for the idle restart, because main.py kills the installer at
    120 s and a step killed from outside cannot undo itself.
    """
    if project_root is None:
        project_root = PROJECT_ROOT
    result = {"applied": False, "version": "", "reason": "", "restored": False}

    try:
        if is_development_copy(project_root):
            result["reason"] = "this is a development copy, so it is never updated automatically"
            return result

        data = project_root / "data"
        pending = None
        try:
            ready = data / "pending_update" / "ready.json"
            if ready.exists():
                pending = json.loads(ready.read_text(encoding="utf-8"))
        except Exception:
            pending = None
        version = staged_update_applicable(pending, running_version(project_root), data)
        if not version:
            result["reason"] = "no update is waiting for this version"
            return result
        result["version"] = version

        zip_path = data / "pending_update" / str(pending.get("zip_filename", ""))
        problem = stage_problem(data, pending, version)
        if problem == "missing":
            # The channel check re-downloads only when no record names this
            # version, so the record goes too -- or it would wait for ever.
            discard_stage(data, pending)
            result["reason"] = (f"the package for version {version} is not downloaded; it will "
                                f"be fetched again on the next check")
            return result
        if problem:
            discard_stage(data, pending)
            if not set_aside_after_failure(data, pending, version, "its package was unusable"):
                log.error(f"automatic update to {version} refused: the staged package does not "
                          f"look like a Vaulter AI release")
            result["reason"] = (f"the staged package for version {version} was refused: {problem}; "
                                f"it will be downloaded again")
            return result

        pip_timeout = STARTUP_PIP_TIMEOUT if at_startup else AUTO_PIP_TIMEOUT
        smoke_timeout = STARTUP_SMOKE_TEST_TIMEOUT if at_startup else SMOKE_TEST_TIMEOUT

        if at_startup:
            deps_change = release_changes_dependencies(project_root, zip_path)
            if deps_change and not _restart_task_gave_up(project_root, version):
                result["reason"] = (f"version {version} changes the dependency list, so the "
                                    f"idle restart will install it")
                return result
            if other_servers_running(project_root):
                result["reason"] = (f"another Vaulter AI server is running, so the idle "
                                    f"restart will install version {version}")
                return result
            if deadline is not None:
                # What is left of the startup budget must hold the smoke test, a
                # margin for the backup and file copy, and (if needed) pip.
                spare = deadline - clock() - smoke_timeout - STARTUP_MARGIN_SECONDS
                if spare < 0 or (deps_change and spare < STARTUP_MIN_PIP_SECONDS):
                    result["reason"] = (f"not enough of the startup time is left to install "
                                        f"version {version} safely, so the idle restart will")
                    return result
                pip_timeout = int(min(pip_timeout, spare))

        lock = data / "apply.lock"
        if not _take_lock(lock):
            result["reason"] = "another update is already in progress"
            return result
        backup = _backup_dir(project_root, version)
        marker = _marker_path(project_root, version)
        try:
            try:
                _backup_tree(project_root, backup, marker, running_version(project_root))
            except Exception as e:
                _discard_backup(backup, marker)
                log.error(f"could not back up before updating to {version} "
                          f"({type(e).__name__}); nothing was changed")
                result["reason"] = f"the backup could not be made, so nothing was changed ({e})"
                return result

            try:
                applied = apply_pending_update(project_root, pip_timeout=pip_timeout)
            except Exception as e:
                log.error(f"automatic update to {version} failed "
                          f"({type(e).__name__}); putting the previous version back")
                result["restored"] = _restore(project_root, backup, marker, version)
                result["reason"] = f"the update failed and was undone ({e})"
                set_aside_after_failure(data, pending, version, "the install failed")
                return result

            if not applied.get("applied"):
                # Nothing was written (no stage, missing file, bad signature).
                _discard_backup(backup, marker)
                result["reason"] = applied.get("reason", "")
                if applied.get("signature_failed"):
                    # Verified when it was downloaded, so the copy here changed
                    # since. Left in place it would block this version for ever
                    # (the channel check sees it as already downloaded).
                    log.warning(f"[APPLY] the staged package for {version} failed its signature "
                                f"check at install time; it is thrown away and will be "
                                f"downloaded again")
                    discard_stage(data, pending)
                    set_aside_after_failure(data, pending, version, "its signature did not verify")
                return result

            if applied.get("dependencies_ok") is False and at_startup \
                    and applied.get("dependencies_timed_out"):
                # Cut off by the startup budget, not refused by pip: put the old
                # version back and leave it for the idle restart (300 s), without
                # counting it against the release (review M-3).
                log.warning(f"[APPLY] the dependencies of {version} did not finish installing "
                            f"within the startup time; the previous version was put back and "
                            f"the idle restart will install it")
                result["restored"] = _restore(project_root, backup, marker, version)
                result["reason"] = (f"the dependencies of version {version} did not finish within "
                                    f"the startup time, so the idle restart will install it")
                return result

            if applied.get("dependencies_ok") is False:
                # The old requirements.txt comes back with the restore, so the
                # next attempt sees a change and runs pip again.
                log.error(f"automatic update to {version}: its dependencies could not be "
                          f"installed; putting the previous version back")
                result["restored"] = _restore(project_root, backup, marker, version)
                result["reason"] = (f"the dependencies of version {version} could not be "
                                    f"installed, so the previous version was put back")
                set_aside_after_failure(data, pending, version, "its dependencies could not be installed")
                return result

            # Can the new code even be imported? Run with the in-progress note
            # in place, so a process killed during it is recovered next start.
            _write_in_progress(project_root, version, str(pending.get("signature") or ""))
            if not _smoke_test(project_root, smoke_timeout):
                log.error(f"automatic update to {version}: the new version could not be "
                          f"started (test import failed); putting the previous version back")
                result["restored"] = _restore(project_root, backup, marker, version)
                result["reason"] = (f"version {version} could not be started, so the previous "
                                    f"version was put back")
                set_aside_after_failure(data, pending, version, "the new version could not be started")
                return result
            _clear_in_progress(project_root)

            _discard_backup(backup, marker)
            result["applied"] = True
            result["reason"] = applied.get("reason", "")
            if at_startup:
                _record_startup_install(project_root, version)
            return result
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as e:
        result["reason"] = f"automatic update could not run ({e})"
        return result


def _only_complete_backup(project_root: Path) -> str:
    """The version of the one complete pre-update backup, or "" (none, or several)."""
    try:
        found = [p.name[len("pre_update_"):-len(".complete")]
                 for p in (project_root / "data" / "backups").glob("pre_update_*.complete")]
    except OSError:
        return ""
    return found[0] if len(found) == 1 else ""


def recover_unfinished_apply(project_root: Path | None = None) -> bool:
    """If an apply began and never finished (marker present) and a backup of the
    version it was replacing exists, put that backup back. True if it did. The
    marker stays so _report_unfinished_apply still tells the team; the backup is
    removed so a second start does not restore over newer files.

    Takes the install lock first: a fresh lock means an install is running right
    now (its in-progress note is expected), so nothing is touched and this
    returns False. A stale lock (its process died) is taken over."""
    if project_root is None:
        project_root = PROJECT_ROOT
    note = _in_progress_path(project_root)
    try:
        if not note.exists():
            return False
        try:
            info = json.loads(note.read_text(encoding="utf-8"))
            info = info if isinstance(info, dict) else {}
        except Exception:
            info = {}
        version = info.get("version")
        if not version:
            # A note emptied or garbled by a process killed while writing it.
            # Exactly one complete backup says which update it was; anything
            # else cannot be told, and that is not an error worth reporting.
            version = _only_complete_backup(project_root)
        if not version:
            log.info("[APPLY] an unfinished-update note could not be read, and no single "
                     "backup matches it; nothing to put back")
            return False
        lock = project_root / "data" / "apply.lock"
        if not _take_lock(lock):
            log.info(f"[APPLY] an update to {version} is in progress elsewhere; not recovering")
            return False
        try:
            backup = _backup_dir(project_root, str(version))
            marker = _marker_path(project_root, str(version))
            if not _backup_is_usable(backup, marker, running_version(project_root), str(version)):
                return False
            _sync_tree(project_root, backup)
            _discard_backup(backup, marker)
            log.error(f"an update to {version} did not finish; the previous version was put back")
            if not info.get("counted"):
                # An install killed part-way (main.py's 120 s, the task's time
                # limit) is a failure like any other: without this it was put
                # back and tried again at every start (review M-2).
                set_aside_after_failure(project_root / "data",
                                        {"version": str(version), "signature": info.get("signature") or None},
                                        str(version), "it did not finish installing")
            return True
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as e:
        log.error(f"could not recover from an unfinished update ({type(e).__name__})")
        return False


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


def _auto_cli(startup: bool, project_root: Path | None = None, *,
              sleep=_time.sleep, clock=_time.monotonic) -> None:
    """`--auto [--startup]`: recover, then install if safe. One line, exit 0.

    At startup, a fresh install lock means the idle task is installing right
    now: wait up to STARTUP_LOCK_WAIT_SECONDS for it to finish, then go on
    (recovery and auto_apply both re-check the lock themselves).

    Logs to data/logs/vaulter.log in the server's own format, because the team's
    error reports scan that file for [ERROR] lines. A plain append handler: the
    server's rotating handler owns rotation and has not opened the log yet.
    """
    if project_root is None:
        project_root = PROJECT_ROOT
    handler = None
    root_logger = logging.getLogger()
    old_level = root_logger.level
    try:
        logs = project_root / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logs / "vaulter.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s  [%(levelname)s]  %(message)s"))
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        deadline = clock() + STARTUP_BUDGET_SECONDS if startup else None
        if startup:
            _wait_for_lock(project_root / "data" / "apply.lock", sleep=sleep, clock=clock)
        recovered = recover_unfinished_apply(project_root)
        r = auto_apply(project_root, at_startup=startup, deadline=deadline, clock=clock)
        extra = " (previous version put back after an unfinished apply)" if recovered else ""
        print(f"auto-update: applied={r['applied']} version={r['version'] or '-'} "
              f"restored={r['restored']} {r['reason']}{extra}".strip())
    except Exception as e:
        print(f"auto-update: error {e}")
    finally:
        if handler is not None:
            root_logger.removeHandler(handler)
            handler.close()
        root_logger.setLevel(old_level)


if __name__ == "__main__":
    if "--auto" in sys.argv[1:]:
        _auto_cli("--startup" in sys.argv[1:])
    else:
        main()
