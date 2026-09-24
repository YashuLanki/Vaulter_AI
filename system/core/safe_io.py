"""
safe_io.py
----------
Shared helpers for reading/writing the small JSON files this project
keeps: the update markers written by scripts/release.py, org settings
from scripts/push_org_setting.py, the setup wizard's saved settings, and
geo_federal.py's lookup cache. (The registries, token cache and screening
manifest this module was first written for were removed in the 2026-07
rebuild.) Nothing here is stage-specific -- this is a cross-cutting
utility, same spirit as config.py.

Two problems this fixes, both present in the original ad-hoc
`json.loads(path.read_text())` / `path.write_text(json.dumps(...))`
pattern:

  1. Corruption from a crash/kill mid-write -- a plain write_text() is
     not atomic; a process killed partway through leaves a truncated
     file that every future load() call chokes on.
  2. Silent data loss on a corrupt file -- loads with no try/except
     (or a bare `except: return {}`) either crash the caller, or
     silently reset to empty and then get overwritten, discarding
     everything previously accumulated with no trace.

The same-machine file lock and OneDrive conflict-copy merging that used
to live here were removed 2026-09-24: their last callers went in the
2026-07 rebuild, and no released version since has used them.
"""

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("vaulter.safe_io")

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
    this way. A caller doing a real read-modify-write should call
    _read_json_or_raise() and let it propagate, so the write is refused
    rather than silently corrupting good data.
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
    Callers that DO write back (read-modify-write) must not treat an
    unreadable file as empty -- see UnreadableFileError."""
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
    """The atomic write-then-rename behind save_json_atomic, usable
    directly for text that is not JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp_path.write_text(text)
    tmp_path.replace(path)
