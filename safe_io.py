"""
safe_io.py
----------
Shared helpers for reading/writing the small JSON registry/cache files
scattered across this project (ingestion/registry.py, pipeline/*.py's
scrape/email registries, pipeline/outlook_auth.py's token cache, and
analysis/screening/'s shared manifest + per-listing/finalist/geocode
caches). Nothing here is stage-specific -- this is a cross-cutting
utility, same spirit as config.py.

Three problems this fixes, all present in the original ad-hoc
`json.loads(path.read_text())` / `path.write_text(json.dumps(...))`
pattern used everywhere:

  1. Corruption from a crash/kill mid-write -- a plain write_text() is
     not atomic; a process killed partway through leaves a truncated
     file that every future load() call chokes on.
  2. Silent data loss on a corrupt file -- loads with no try/except
     (or a bare `except: return {}`) either crash the whole pipeline
     stage, or silently reset to empty and then get overwritten,
     discarding everything previously accumulated with no trace.
  3. Read-modify-write races between two processes on the same machine
     (e.g. a manual run overlapping with the scheduler) -- whichever
     process's write lands last silently wins, discarding the other's
     changes.

locked_json_update() fixes all three at once for call sites that need a
real read-modify-write. load_json()/save_json_atomic() are available
standalone for simpler load-then-overwrite call sites.

NOTE on files shared across the team via OneDrive (config.SHARED_DIR):
the file lock here is a local, same-machine lock (a `<path>.lock`
sidecar, via the filelock library) -- it fully eliminates races between
processes on the SAME computer, but does NOT and CANNOT prevent two
DIFFERENT team members' machines from writing at the exact same instant,
since OneDrive syncs independent local copies rather than providing a
real shared/networked filesystem with cross-machine lock semantics.
Re-reading the file at the last possible moment (immediately before
writing, inside the lock) minimizes that cross-machine window to as
small as practically possible, but does not close it entirely.
"""

import json
import logging
import os
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout

log = logging.getLogger("vaulter.safe_io")

DEFAULT_LOCK_TIMEOUT_SECONDS = 30


def load_json(path: Path, default=None):
    """Safely load a JSON file. Returns `default` (a fresh {} if not
    given) if the file doesn't exist or fails to parse -- logging a
    warning in the corrupt case so a torn write is visible in the logs
    instead of silently and invisibly resetting to empty."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.warning(
            f"Could not read {path} ({e}) -- treating as empty. If this "
            f"keeps happening, check for a process that was killed mid-write "
            f"or a sync conflict, and consider restoring from a backup."
        )
        return default


def save_json_atomic(path: Path, data) -> None:
    """Writes data as JSON to path atomically: writes to a temp file in
    the same directory, then renames over the real file. Path.replace()
    is atomic on both POSIX and Windows for a same-filesystem rename, so
    a crash/kill mid-write leaves the ORIGINAL file untouched rather than
    a truncated, corrupt one."""
    save_text_atomic(path, json.dumps(data, indent=2))


def save_text_atomic(path: Path, text: str) -> None:
    """Same atomic write-then-rename as save_json_atomic, for callers with
    their own non-JSON serialization (e.g. MSAL's token cache, which
    serializes to its own opaque string format via cache.serialize())."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp_path.write_text(text)
    tmp_path.replace(path)


def dedupe_path(dest_dir: Path, filename: str) -> Path:
    """
    Return a path inside dest_dir that won't collide with an existing
    file. shutil.move()/shutil.copy() silently overwrite a same-named
    destination -- this appends a numeric suffix instead, so two
    different documents that happen to share a filename (two separate
    "survey.pdf" drops for the same property, two email attachments both
    named "invoice.pdf", etc.) don't destroy one another.
    """
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate

    stem, suffix = Path(filename).stem, Path(filename).suffix
    n = 2
    while (dest_dir / f"{stem}_{n}{suffix}").exists():
        n += 1
    return dest_dir / f"{stem}_{n}{suffix}"


def locked_json_update(path: Path, update_fn: Callable[[dict], dict], default=None,
                        timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> dict:
    """
    Read-modify-write a JSON file under an exclusive, same-machine file
    lock, so two processes on this computer (a manual run and the
    scheduler, two scheduled jobs overlapping, etc.) can't race and
    silently discard each other's changes.

    update_fn receives the current dict (freshly re-read from disk,
    inside the lock -- so it reflects any change another process just
    made) and must return the new dict to save.

    Raises TimeoutError if the lock can't be acquired within `timeout`
    seconds (e.g. another process hung while holding it) -- callers
    should let this propagate rather than silently skip the update.
    """
    if default is None:
        default = {}
    lock_path = str(path) + ".lock"
    try:
        with FileLock(lock_path, timeout=timeout):
            current = load_json(path, default=default)
            updated = update_fn(current)
            save_json_atomic(path, updated)
            return updated
    except Timeout:
        raise TimeoutError(
            f"Could not acquire lock on {path} within {timeout}s -- "
            f"another process may be stuck holding it."
        )
