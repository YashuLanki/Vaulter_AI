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
import time
from pathlib import Path
from typing import Callable

from filelock import FileLock, Timeout

log = logging.getLogger("vaulter.safe_io")

DEFAULT_LOCK_TIMEOUT_SECONDS = 30

# A file that exists but fails to parse is retried this many times, this
# far apart, before being treated as genuinely unreadable -- long enough
# to ride out a torn read caught mid-sync (e.g. OneDrive writing a new
# copy of a shared file), short enough not to meaningfully delay a caller.
UNREADABLE_RETRY_ATTEMPTS = 3
UNREADABLE_RETRY_DELAY_SECONDS = 0.3


class UnreadableFileError(Exception):
    """
    Raised when a JSON file exists on disk but can't be parsed, even
    after retrying to ride out a transient torn read. This is
    deliberately a distinct case from "the file doesn't exist" --
    conflating the two (as this module used to) is exactly how a shared
    team file gets silently wiped: a caller doing a read-modify-write
    that misreads "present but currently unreadable" as "empty" goes on
    to overwrite it with a near-empty file, discarding everyone else's
    already-synced data. See C1 in docs/MULTI_USER_TRANSITION.md.

    load_json() catches this internally and falls back to `default`,
    since a plain read that doesn't write anything back can't lose data
    this way. locked_json_update() deliberately does NOT catch it --
    callers doing a real read-modify-write must let it propagate so the
    write is refused rather than silently corrupting good data.
    """


def _read_json_or_raise(path: Path):
    """Read and parse `path` as JSON, retrying briefly on failure to ride
    out a transient torn read before giving up. Raises
    UnreadableFileError (never silently returns something else) if the
    file exists but still can't be parsed after retrying -- the caller
    decides what "can't trust this as empty" means for it, rather than
    this function silently deciding for them."""
    last_error = None
    for attempt in range(UNREADABLE_RETRY_ATTEMPTS):
        try:
            return json.loads(path.read_text())
        except Exception as e:
            last_error = e
            if attempt < UNREADABLE_RETRY_ATTEMPTS - 1:
                time.sleep(UNREADABLE_RETRY_DELAY_SECONDS)
    raise UnreadableFileError(
        f"{path} exists but could not be parsed as JSON after "
        f"{UNREADABLE_RETRY_ATTEMPTS} attempts ({last_error}). This usually means a sync "
        f"tool (e.g. OneDrive) caught it mid-write; if it keeps happening, check "
        f"whether the file is genuinely corrupt (e.g. restore from OneDrive version "
        f"history)."
    )


def load_json(path: Path, default=None):
    """Safely load a JSON file. Returns `default` (a fresh {} if not
    given) if the file doesn't exist, or still fails to parse after a
    few retries -- logging a warning in the latter case so a torn read is
    visible in the logs instead of silently and invisibly resetting to
    empty. Safe to treat as "just return default" here specifically
    because this function never writes anything back -- a stale/empty
    read only risks an occasional avoidable cache miss, never data loss.
    Callers that DO write back (read-modify-write) must use
    locked_json_update() instead, which refuses to do this."""
    if default is None:
        default = {}
    if not path.exists():
        return default
    try:
        return _read_json_or_raise(path)
    except UnreadableFileError as e:
        log.warning(f"{e} -- treating as empty for this read (nothing is written back "
                    f"here, so no data is at risk).")
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

    Raises UnreadableFileError if `path` exists but can't be parsed even
    after retrying -- e.g. caught mid-write by a sync tool like OneDrive,
    or genuinely corrupt. This is intentional, not a bug: silently
    treating an unreadable file as "empty" and writing update_fn's result
    back would overwrite whatever's actually on disk with a near-empty
    file, discarding it. Callers should let this propagate too -- see C1
    in docs/MULTI_USER_TRANSITION.md for the data-loss incident this
    prevents. A caller for whom aborting the whole operation over this is
    too costly (e.g. after an expensive Claude/Google API call already
    succeeded) should catch it specifically and decide its own fallback
    -- see analysis/screening/pipeline.py, phase3_deep_analysis.py, and
    phase4_verification.py for examples.
    """
    if default is None:
        default = {}
    lock_path = str(path) + ".lock"
    try:
        with FileLock(lock_path, timeout=timeout):
            current = _read_json_or_raise(path) if path.exists() else default
            updated = update_fn(current)
            save_json_atomic(path, updated)
            return updated
    except Timeout:
        raise TimeoutError(
            f"Could not acquire lock on {path} within {timeout}s -- "
            f"another process may be stuck holding it."
        )


def merge_conflict_copies(path: Path, merge_fn: Callable[[dict, dict], dict],
                           timeout: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> int:
    """
    Finds and reconciles OneDrive conflict-copy files for `path`.

    When two different machines write to the same shared file (in
    config.SHARED_DIR) at close to the same moment, OneDrive can't merge
    the changes and keeps both: the "official" file at `path`, and a
    renamed copy with the conflicting device's name appended -- e.g.
    "manifest-JOHNS-SURFACE.json" alongside "manifest.json" (confirmed
    behavior for OneDrive for work/school with non-Office file types,
    which creates up to 5 such copies -- see Microsoft's own docs:
    https://learn.microsoft.com/en-us/troubleshoot/sharepoint/sync/
    troubleshoot-sync-issues). Nothing ever reads these renamed copies on
    their own, so whatever entry the "losing" side just saved silently
    vanishes from the shared record -- see C2 in
    docs/MULTI_USER_TRANSITION.md.

    For each conflict copy found (matching "<path.stem>-*<path.suffix>"),
    this reads it, merges its contents into the official file via
    merge_fn(official_dict, conflict_copy_dict) -- called under the same
    lock as the write via locked_json_update(), so this is safe to run
    alongside any other locked_json_update() caller for this same path --
    and deletes the conflict copy once it's been folded in. merge_fn must
    return the merged dict; a plain union is safe here specifically
    because every caller's entries are uniquely keyed (a market/hash/
    top_n combo, or a content-hash cache key), so merging can't silently
    clobber one machine's entry with another's.

    An unreadable conflict copy (itself caught mid-sync) or one that
    can't currently be merged (e.g. the official file is momentarily
    unreadable too) is left in place, not deleted, so a future call can
    retry it -- never delete a file this function hasn't successfully
    folded in yet.

    Returns how many conflict copies were merged and removed.
    """
    merged_count = 0
    for candidate in sorted(path.parent.glob(f"{path.stem}-*{path.suffix}")):
        try:
            conflict_data = _read_json_or_raise(candidate)
        except UnreadableFileError as e:
            log.warning(f"Found a possible OneDrive conflict copy {candidate.name}, but "
                        f"couldn't read it yet ({e}) -- will retry next time.")
            continue

        try:
            locked_json_update(path, lambda current, cd=conflict_data: merge_fn(current, cd),
                                timeout=timeout)
        except (UnreadableFileError, TimeoutError) as e:
            log.warning(f"Found OneDrive conflict copy {candidate.name}, but couldn't merge "
                        f"it into {path.name} right now ({e}) -- will retry next time.")
            continue

        candidate.unlink()
        merged_count += 1
        log.info(f"Merged and removed OneDrive conflict copy: {candidate.name}")

    return merged_count
