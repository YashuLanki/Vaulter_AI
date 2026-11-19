"""
apply_update.py
----------------
Vaulter AI — apply a staged update (Priority 4 in
docs/MULTI_USER_TRANSITION.md).

The primary, non-technical way to use this is to just ask Claude in a
normal conversation once check_system_health mentions an update is
ready ("go ahead and install it") -- the apply_pending_update MCP tool
(see mcp_server.py) calls straight into apply_pending_update() below,
no terminal needed. This file's own command-line entry point
(`python apply_update.py`) is kept as a manual/troubleshooting fallback,
not the expected everyday path.

This is deliberately a human-confirmed step, not something that runs on
its own -- nothing in this project downloads AND applies an update
without that confirmation; the scheduler only ever stages one (see
mcp_server.py's _check_and_stage_update). Applying means:
  1. Extracting the staged code package to a temp folder.
  2. Deleting any file in this project that the new version no longer
     has (so removed files don't linger as stale dead code).
  3. Copying every file from the new version into place.
  4. Re-installing from requirements.txt with the same Python already
     running this project, in case the update added/changed a
     dependency -- otherwise the new code could reference a package
     that was never installed.
  5. Cleaning up the staging area.

confidentials/ and data/ are NEVER touched -- the update package itself
never contains them (see release.py's exclusion list), so this can't
and doesn't delete or overwrite your secrets or local data. The virtual
environment's FILES aren't touched either, though step 4 does install
packages into it, same as any other `pip install`.

You'll need to restart Claude Desktop (and the MCP server it launches)
afterward for the new code to actually take effect -- nothing here does
that for you, since Claude Desktop can't be restarted from inside its
own MCP server process.
"""

import base64
import logging
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


def _sync_tree(project_root: Path, src_dir: Path) -> tuple[int, int]:
    """
    Make project_root match src_dir exactly, except for anything under a
    PRESERVED_DIR_NAMES path: delete files src_dir lacks, copy every file
    src_dir has, drop now-empty folders. Returns (copied, deleted). Shared by
    the apply and by the restore of a pre-update backup, so both behave alike.
    """
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
            existing.unlink()
            deleted += 1

    # Step 2: copy every file from the new version into place.
    updated = 0
    for rel in new_files:
        src = src_dir / rel
        dest = project_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        updated += 1

    # Remove any now-empty directories left behind by step 1 (never
    # inside a preserved path, and never the project root itself).
    for d in sorted(project_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if d.is_dir() and not _is_preserved(d.relative_to(project_root)) and not any(d.iterdir()):
            d.rmdir()

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

def refresh_dependencies(project_root: Path) -> tuple[bool, str]:
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
                stdin=subprocess.DEVNULL, text=True, timeout=600,
            )
        output = out_path.read_text(encoding="utf-8", errors="replace")
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
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
            capture_output=True, text=True, timeout=600,
            # stdin closed, not inherited. Under MCP this process's stdin is
            # the pipe Claude Desktop talks to us on, and capture_output only
            # redirects the two OUTPUT streams. A pip that reads stdin blocks
            # on a pipe that never answers. Identical hole to the one that
            # cost 10s on every conversation via git, fixed the same day.
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-2000:]
    return True, ""


def apply_pending_update(project_root: Path = None) -> dict:
    """
    Applies whatever update is currently staged, with NO interactive
    prompt -- callers (this file's own main(), or the
    apply_pending_update MCP tool in mcp_server.py) are responsible for
    getting a human's explicit go-ahead BEFORE calling this; this
    function just does the work once called. Never raises for the
    ordinary "nothing staged" / "staged file missing" cases -- always
    returns a result dict describing what happened.
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
            deps_ok, deps_message = refresh_dependencies(project_root)
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
    }


# ---------------------------------------------------------------------------
# Automatic install (2026-11-19)
#
# Everything below derives its paths from project_root (the `system` folder)
# using config's own layout, NOT from config's constants, so it can be pointed
# at a scratch tree. apply_pending_update() above still reads config itself.
# ---------------------------------------------------------------------------

LOCK_STALE_SECONDS = 600


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


def staged_update_applicable(pending, running: str) -> str:
    """The version worth installing, or "". Not a dict, no version, the version
    already running, or a stage made while a different version was running (the
    machine has since moved on, so installing it could be a downgrade)."""
    if not isinstance(pending, dict):
        return ""
    version = pending.get("version")
    if not version or version == running:
        return ""
    at_download = pending.get("current_version_at_download")
    if at_download and running and at_download != running:
        return ""
    return str(version)


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


def other_servers_running(project_root: Path) -> bool:
    """True if another MCP server from this install is running. Windows only;
    False on any failure (the caller then proceeds -- a missed server costs one
    update applied under it, which the backup covers). Output goes to a file,
    never a pipe, for the reason refresh_dependencies gives."""
    if sys.platform != "win32":
        return False
    import os
    out_path = Path(tempfile.gettempdir()) / f"vaulter_procs_{os.getpid()}.json"
    try:
        script = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
                  "Select-Object ProcessId, ParentProcessId, CommandLine | ConvertTo-Json -Compress")
        with open(out_path, "w", encoding="utf-8") as fh:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=fh, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=20,
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
    marker.unlink(missing_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
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
        shutil.copy2(p, target)
        count += 1
    marker.write_text(json.dumps({"source_version": source_version, "files": count}),
                      encoding="utf-8")


def _discard_backup(dest: Path, marker: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def _backup_is_usable(dest: Path, marker: Path, current_version: str) -> bool:
    """Complete (marker present, file count matches) and made from the version
    now installed -- a backup of some older tree must not be put over a newer one."""
    try:
        info = json.loads(marker.read_text(encoding="utf-8"))
        if not dest.is_dir() or info.get("source_version") != current_version:
            return False
        return sum(1 for p in dest.rglob("*") if p.is_file()) == info.get("files")
    except Exception:
        return False


def _take_lock(lock: Path) -> bool:
    import os
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = _time.time() - lock.stat().st_mtime
            except OSError:
                continue
            if age > LOCK_STALE_SECONDS:
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


def auto_apply(project_root: Path | None = None, *, at_startup: bool = False) -> dict:
    """
    Install the staged update without asking, but only when that is safe.
    Never raises. Returns {"applied", "version", "reason", "restored"}.

    At startup (the server is about to start serving) an update that changes
    dependencies, or one landing while another server from this install runs,
    is left for the idle restart: pip must not run under a live server.
    Every file is backed up first and put back if the apply fails.
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
        version = staged_update_applicable(pending, running_version(project_root))
        if not version:
            result["reason"] = "no update is waiting for this version"
            return result
        result["version"] = version

        if at_startup:
            zip_path = data / "pending_update" / str(pending.get("zip_filename", ""))
            if release_changes_dependencies(project_root, zip_path):
                result["reason"] = (f"version {version} changes the dependency list, so the "
                                    f"idle restart will install it")
                return result
            if other_servers_running(project_root):
                result["reason"] = (f"another Vaulter AI server is running, so the idle "
                                    f"restart will install version {version}")
                return result

        lock = data / "apply.lock"
        if not _take_lock(lock):
            result["reason"] = "another update is already in progress"
            return result
        backup = _backup_dir(project_root, version)
        marker = _marker_path(project_root, version)
        try:
            _backup_tree(project_root, backup, marker, running_version(project_root))
        except Exception as e:
            _discard_backup(backup, marker)
            log.error(f"[ERROR] could not back up before updating to {version} "
                      f"({type(e).__name__}: {e}); nothing was changed")
            result["reason"] = f"the backup could not be made, so nothing was changed ({e})"
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
            return result
        try:
            try:
                applied = apply_pending_update(project_root)
            except Exception as e:
                log.error(f"[ERROR] automatic update to {version} failed "
                          f"({type(e).__name__}: {e}); putting the previous version back")
                try:
                    _sync_tree(project_root, backup)
                    result["restored"] = True
                    _discard_backup(backup, marker)
                except Exception as e2:
                    log.error(f"[ERROR] could not put the previous version back: {e2}")
                result["reason"] = f"the update failed and was undone ({e})"
                return result
            result["applied"] = bool(applied.get("applied"))
            result["reason"] = applied.get("reason", "")
            _discard_backup(backup, marker)
            return result
        finally:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception as e:
        result["reason"] = f"automatic update could not run ({e})"
        return result


def recover_unfinished_apply(project_root: Path | None = None) -> bool:
    """If an apply began and never finished (marker present) and a backup of the
    version it was replacing exists, put that backup back. True if it did. The
    marker stays so _report_unfinished_apply still tells the team; the backup is
    removed so a second start does not restore over newer files."""
    if project_root is None:
        project_root = PROJECT_ROOT
    marker = project_root / "data" / "apply_in_progress.json"
    try:
        if not marker.exists():
            return False
        version = json.loads(marker.read_text(encoding="utf-8")).get("version")
        if not version:
            return False
        backup = _backup_dir(project_root, str(version))
        marker = _marker_path(project_root, str(version))
        if not _backup_is_usable(backup, marker, running_version(project_root)):
            return False
        _sync_tree(project_root, backup)
        _discard_backup(backup, marker)
        log.error(f"[ERROR] an update to {version} did not finish; "
                  f"the previous version was put back")
        return True
    except Exception as e:
        log.error(f"[ERROR] could not recover from an unfinished update: {e}")
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


def _auto_cli(startup: bool) -> None:
    """`--auto [--startup]`: recover, then install if safe. One line, exit 0."""
    try:
        logs = PROJECT_ROOT / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(filename=str(logs / "auto_update.log"), level=logging.INFO,
                            format="%(asctime)s %(message)s")
        recovered = recover_unfinished_apply()
        r = auto_apply(at_startup=startup)
        extra = " (previous version put back after an unfinished apply)" if recovered else ""
        print(f"auto-update: applied={r['applied']} version={r['version'] or '-'} "
              f"restored={r['restored']} {r['reason']}{extra}".strip())
    except Exception as e:
        print(f"auto-update: error {e}")


if __name__ == "__main__":
    if "--auto" in sys.argv[1:]:
        _auto_cli("--startup" in sys.argv[1:])
    else:
        main()
