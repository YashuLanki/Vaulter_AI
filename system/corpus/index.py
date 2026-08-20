"""
corpus/index.py
---------------
Locating documents in the firm's library without downloading them.

Why an index exists at all
--------------------------
The library is synced by OneDrive Files On-Demand. Most files are
*placeholders*: the directory entry and filename are local, the bytes are not.
That has two consequences this module is built around.

  * Walking the tree live is slow. Every stat() goes through the OneDrive
    filter driver. A full `find` over the library did not finish in five
    minutes during development.
  * Reading file contents triggers a download. Grepping the corpus for a
    keyword would hydrate every file it touched -- potentially the entire
    library, gigabytes of it, one Claude question at a time.

So: this module never reads file contents. It walks names once, caches them
locally in `config.CORPUS_INDEX_FILE`, and searches that cache. Content is read
only by `corpus.extract.read_document`, on a specific file a human or Claude
deliberately picked out of search results.

The cache is SQLite. The library turned out to hold roughly 400,000 files --
a JSON index of that is ~60MB, and re-parsing it on every search would cost
seconds per query. SQLite is in the standard library, so this costs no new
dependency. Search is a full scan (LIKE '%term%' can't use an index), but a
scan of 400k short rows is milliseconds.

This means search is over **paths and filenames**, not full text. In this
library that is much less of a downgrade than it sounds -- the naming
convention is dense and consistent (`220419 Neighboring Hotel Public Hearing
Notice.pdf` under `!PROPERTIES/ARIZONA/<Property>/`), so the path usually
carries the property, the date, the counterparty, and the document kind.
"""

import logging
import os
import sqlite3
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CORPUS_DIR, CORPUS_INDEX_FILE

log = logging.getLogger("vaulter.corpus")


class CorpusUnavailable(RuntimeError):
    """CORPUS_DIR is not present -- OneDrive isn't syncing the library here."""


class OutsideCorpus(ValueError):
    """A path was requested that resolves outside CORPUS_DIR."""


# Windows file attributes that mark a OneDrive Files On-Demand placeholder.
# stat only names OFFLINE; the recall flags are not exposed as constants.
_FILE_ATTRIBUTE_RECALL_ON_OPEN        = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_PLACEHOLDER_FLAGS = (
    stat.FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

# Skipped wholesale when indexing. OneDrive/Office scratch, and Windows
# shortcuts (the library has dozens of stale .lnk files pointing at a
# decommissioned file server -- they resolve to nothing useful).
# "Vaulter AI Shared" may live INSIDE the document library, so that a teammate
# gets the team's data automatically instead of having to be sent a folder and
# add a OneDrive shortcut. It sits in the library on disk but is emphatically
# NOT part of the document corpus -- it holds this system's own output, and
# indexing it would mean screening workbooks and proximity CSVs turning up when
# someone searches for a closing memo. Skipping it here is the whole reason
# that arrangement is safe; do not remove this without moving the folder out.
#
# The shared folder is skipped by whatever name config actually DETECTED it
# under, not only the canonical literal: config._detect_shared_dir can land on
# a renamed variant ("Vaulter AI Shared 1" after a shortcut collision) or an
# operator-set VAULTER_SHARED_DIR, and a literal-only skip would quietly index
# the team's output on exactly those machines. The literal stays as a
# belt-and-braces floor for the common case.
_SKIP_DIR_NAMES  = {".git", "$RECYCLE.BIN", "System Volume Information",
                    "Vaulter AI Shared"}
try:
    from config import SHARED_DIR as _SHARED_DIR
    if _SHARED_DIR:
        _SKIP_DIR_NAMES.add(Path(_SHARED_DIR).name)
except ImportError:
    pass
_SKIP_SUFFIXES   = {".lnk", ".url", ".tmp", ".ini"}
_SKIP_PREFIXES   = ("~$", ".~")


# ─── Scope guard ──────────────────────────────────────────────────────────────

def _corpus_root() -> Path:
    if CORPUS_DIR is None or not CORPUS_DIR.is_dir():
        raise CorpusUnavailable(
            "The firm's document library isn't available on this machine. "
            "Expected it at: "
            f"{CORPUS_DIR if CORPUS_DIR else '<OneDrive not found>'}. "
            "Check that OneDrive is running and syncing the firm's document "
            "library, or set VAULTER_CORPUS_DIR in confidentials/.env."
        )
    return CORPUS_DIR


def resolve_in_corpus(rel_path: str | Path) -> Path:
    """
    Resolve a corpus-relative path to an absolute one, refusing anything that
    escapes CORPUS_DIR.

    This is the privacy boundary. The parent of CORPUS_DIR is the individual's
    own OneDrive account root -- their Desktop, Documents, and Teams chat
    files. A path like "../Documents" or an absolute path elsewhere on disk
    must never resolve, no matter who or what asked for it.
    """
    root = _corpus_root().resolve()
    candidate = Path(rel_path)
    absolute = (candidate if candidate.is_absolute() else root / candidate).resolve()

    # is_relative_to compares the resolved forms, so symlinks, junctions, and
    # ".." segments are all already collapsed by the time we check.
    if absolute != root and not absolute.is_relative_to(root):
        raise OutsideCorpus(
            f"Refusing to read '{rel_path}': it resolves outside the firm's "
            f"document library. Only files under {root.name} are readable."
        )
    return absolute


def is_online_only(path: Path) -> bool:
    """True if this file is a OneDrive placeholder (opening it downloads it)."""
    try:
        attrs = os.stat(path, follow_symlinks=False).st_file_attributes
    except (AttributeError, OSError):
        return False  # non-Windows, or unreadable -- assume local
    return bool(attrs & _PLACEHOLDER_FLAGS)


# ─── Indexing ─────────────────────────────────────────────────────────────────

def _should_skip(name: str) -> bool:
    lowered = name.lower()
    return (
        name.startswith(_SKIP_PREFIXES)
        or any(lowered.endswith(suffix) for suffix in _SKIP_SUFFIXES)
    )


# Anything the walk could NOT reach, counted rather than merely logged. Windows
# refuses paths over 260 characters unless long-path support is switched on, and
# this library has tens of thousands of files past that. On a machine without it
# the walk simply cannot enter those folders -- and until 2026-08-17 that was a
# warning scrolling past, with the run still reporting a confident total and a
# tick. An index quietly missing a slice of the library is the worst shape this
# system can be in: it produces "no documents found for that property" as a
# fast, confident, wrong answer. Same rule as everywhere else here -- "couldn't
# check" must never be reported as "nothing there".
_UNREACHABLE = {"folders": 0, "files": 0}


def _long_path_safe(root: Path) -> Path:
    r"""
    The same folder, in a form Windows will read no matter how long the paths
    inside it are.

    Windows refuses any path over 260 characters unless a system-wide setting
    is switched on -- and that setting is off by default. Prefixing the root
    with \\?\ opts this walk out of the limit without needing the setting, and
    without administrator rights.

    Found on a real teammate's machine 2026-08-20: her indexing run scrolled
    thousands of "cannot find the path specified" skips, while the maintainer's
    machine indexed the same library with nothing skipped at all. The
    difference was that one Windows setting, switched on here and off there --
    so the bug was invisible from this side, which is the fifth time in two days
    that a working machine has hidden a real fault.

    Left alone on Mac and Linux, which have no such limit, and left alone for a
    path already carrying the prefix.
    """
    if os.name != "nt":
        return root
    prefix = "\\\\?\\"
    text = str(root)
    if text.startswith(prefix):
        return root
    # Must be absolute with real backslashes; the prefix disables all path
    # parsing, so a relative path or a forward slash would simply not resolve.
    text = os.path.abspath(text)
    if text.startswith("\\\\"):                 # a network share
        return Path(prefix + "UNC" + text[1:])
    return Path(prefix + text)


def _walk(root: Path, progress_every: int):
    """Yield (relative_path, name, size, mtime) for every indexable file."""
    seen = 0

    def _unreachable(err):
        _UNREACHABLE["folders"] += 1
        log.warning(f"  skip {err}")

    for dirpath, dirnames, filenames in os.walk(root, onerror=_unreachable):
        # Prune in place so os.walk doesn't descend into them at all.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(_SKIP_PREFIXES)]

        here = Path(dirpath)
        for filename in filenames:
            if _should_skip(filename):
                continue
            try:
                st = (here / filename).stat()
            except OSError:
                _UNREACHABLE["files"] += 1
                continue
            rel = str((here / filename).relative_to(root)).replace("\\", "/")
            yield rel, filename, st.st_size, int(st.st_mtime)
            seen += 1
            if progress_every and seen % progress_every == 0:
                log.info(f"  indexed {seen:,} files...")


def build_index(progress_every: int = 25000) -> dict:
    """
    Walk the library and cache every file's path, size, and mtime.

    Names only -- this never opens a file, so it does not hydrate anything.
    Slow on a cold OneDrive cache (minutes, not seconds) but only needs
    re-running when documents are added or moved.

    Builds into a temporary database and swaps it in at the end, so an
    interrupted run leaves the previous index intact rather than a half-built
    one that would silently return incomplete results.
    """
    _UNREACHABLE.update(folders=0, files=0)
    # Walked through the long-path-safe form so deeply nested folders are read
    # rather than skipped. The paths STORED stay relative and ordinary, so
    # nothing downstream ever sees the prefix.
    root = _long_path_safe(_corpus_root())
    started = time.monotonic()

    CORPUS_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CORPUS_INDEX_FILE.with_suffix(".building")
    tmp_path.unlink(missing_ok=True)

    con = sqlite3.connect(tmp_path)
    try:
        con.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous  = OFF;
            CREATE TABLE files (path TEXT, name TEXT, size INTEGER, mtime INTEGER);
            CREATE TABLE meta  (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        con.executemany(
            "INSERT INTO files (path, name, size, mtime) VALUES (?, ?, ?, ?)",
            _walk(root, progress_every),
        )
        count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        elapsed = time.monotonic() - started
        con.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("root", str(root)),
                ("built_at", datetime.now(timezone.utc).isoformat()),
                ("build_seconds", str(round(elapsed, 1))),
                ("file_count", str(count)),
            ],
        )
        con.commit()
    finally:
        con.close()

    tmp_path.replace(CORPUS_INDEX_FILE)
    log.info(f"Indexed {count:,} files in {elapsed:.0f}s -> {CORPUS_INDEX_FILE}")
    return {"root": str(root), "file_count": count, "build_seconds": round(elapsed, 1),
            "unreachable_folders": _UNREACHABLE["folders"],
            "unreachable_files": _UNREACHABLE["files"]}


def _connect() -> sqlite3.Connection | None:
    """Open the index read-only, or None if it's missing/unusable/stale-rooted."""
    if not CORPUS_INDEX_FILE.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{CORPUS_INDEX_FILE}?mode=ro", uri=True)
        rows = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error as e:
        log.warning(f"Corpus index unreadable ({e}) -- treating as missing.")
        return None

    # An index built against a different root (someone changed
    # VAULTER_CORPUS_DIR, or moved machines) describes files that aren't there.
    if CORPUS_DIR and rows.get("root") != str(CORPUS_DIR):
        log.warning("Corpus index was built for a different root -- ignoring it.")
        con.close()
        return None
    return con


def index_age() -> tuple[int, datetime] | None:
    """(file_count, built_at) for the cached index, or None if there isn't one."""
    con = _connect()
    if con is None:
        return None
    try:
        rows = dict(con.execute("SELECT key, value FROM meta").fetchall())
        return int(rows["file_count"]), datetime.fromisoformat(rows["built_at"])
    except (sqlite3.Error, KeyError, ValueError):
        return None
    finally:
        con.close()


# ─── Search ───────────────────────────────────────────────────────────────────

def _escape_like(term: str) -> str:
    """Escape LIKE wildcards so a literal % or _ in a query doesn't match all."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search(query: str, limit: int = 40, subtree: str = "") -> list[dict]:
    """
    Find documents whose path or filename matches every term in `query`.

    Searches names only -- see this module's header for why. Returns dicts of
    {path, name, size, mtime, online_only, score}, best match first.

    Scoring happens in SQL, on three signals:

      * every term must appear somewhere in the path (AND, not OR);
      * a term found in the filename beats one found only in a parent folder,
        so "coolidge testing proposal" surfaces the document actually named
        that ahead of files that merely live in a Coolidge folder;
      * the whole query appearing as a contiguous phrase outranks both.

    The phrase bonus is not a nicety. Searching "<Name> 10" without it put
    the adjacent "<Name> *80*" parcel's files on top, because the term "10"
    matches inside any date like "20260107". Adjacent numbered parcels are
    exactly the case that has to come out right.

    Args:
        query:   space-separated terms, all of which must match
        limit:   maximum results
        subtree: optional corpus-relative folder to restrict the search to,
                 e.g. "!PROPERTIES/ARIZONA"
    """
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return []

    con = _connect()
    if con is None:
        raise CorpusUnavailable(
            "No corpus index has been built yet, so document search can't run. "
            "Easiest fix: double-click \"Setup Vaulter AI\" in the quick_start "
            "folder, which builds it for you. From a terminal it is: "
            "python system/main.py index-corpus  (takes a few minutes -- it "
            "reads filenames only, never file contents)."
        )

    try:
        where, score_parts, params = [], [], []
        for term in terms:
            pattern = f"%{_escape_like(term)}%"
            where.append("LOWER(path) LIKE ? ESCAPE '\\'")
            params.append(pattern)
            score_parts.append("(CASE WHEN LOWER(name) LIKE ? ESCAPE '\\' THEN 10 ELSE 3 END)")

        # Placeholders bind in the order they appear in the SQL text, and the
        # score expression sits in the SELECT clause -- ahead of the WHERE.
        # So the score patterns must come first in `bind`.
        score_bind = [f"%{_escape_like(t)}%" for t in terms]

        if len(terms) > 1:
            phrase = f"%{_escape_like(' '.join(terms))}%"
            score_parts.append(
                "(CASE WHEN LOWER(name) LIKE ? ESCAPE '\\' THEN 100 "
                "      WHEN LOWER(path) LIKE ? ESCAPE '\\' THEN 50 ELSE 0 END)"
            )
            score_bind += [phrase, phrase]

        score_sql = " + ".join(score_parts)
        sql = (
            f"SELECT path, name, size, mtime, {score_sql} AS score "
            f"FROM files WHERE {' AND '.join(where)}"
        )
        bind = score_bind + params

        prefix = subtree.strip().strip("/").replace("\\", "/").lower()
        if prefix:
            sql += " AND LOWER(path) LIKE ? ESCAPE '\\'"
            bind.append(f"{_escape_like(prefix)}/%")

        sql += " ORDER BY score DESC, mtime DESC LIMIT ?"
        bind.append(limit)

        rows = con.execute(sql, bind).fetchall()
    finally:
        con.close()

    root = _corpus_root()
    return [
        {
            "path": path,
            "name": name,
            "size": size,
            "mtime": mtime,
            # Checked live rather than from the index: whether a file is
            # hydrated changes as people open things, and a stale "downloads
            # on read" warning is worse than no warning.
            "online_only": is_online_only(root / path),
            "score": score,
        }
        for path, name, size, mtime, score in rows
    ]


def list_dir(rel_path: str = "") -> dict:
    """
    List one folder's immediate children. Goes to the live filesystem rather
    than the index -- listing a directory is cheap even on placeholders, and
    this way it reflects anything added since the last index build.
    """
    target = resolve_in_corpus(rel_path)
    if not target.is_dir():
        raise NotADirectoryError(f"Not a folder in the library: {rel_path}")

    folders, files = [], []
    with os.scandir(target) as entries:
        for entry in entries:
            if _should_skip(entry.name):
                continue
            if entry.is_dir():
                folders.append(entry.name)
            else:
                try:
                    st = entry.stat()
                    size, mtime = st.st_size, int(st.st_mtime)
                except OSError:
                    size, mtime = 0, 0
                files.append({"name": entry.name, "size": size, "mtime": mtime})

    folders.sort(key=str.lower)
    files.sort(key=lambda f: f["name"].lower())
    return {"path": rel_path.strip("/"), "folders": folders, "files": files}
