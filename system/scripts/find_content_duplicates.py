"""
find_content_duplicates.py
--------------------------
Find files that are the SAME FILE under DIFFERENT NAMES.

    python system/scripts/find_content_duplicates.py

`find_duplicates.py` matches on name plus exact size, which is free but misses
the copy somebody renamed -- "Appendix A.pdf" and "Appendix A (final).pdf" and
"scan0041.pdf" can be one document filed three ways. Catching those means
comparing the actual contents, so this reads files where the other script only
read the list of names.

TWO RULES IT WILL NOT BREAK:

1. **It only ever opens a file already on this disk.** The library is mostly
   cloud placeholders; opening one downloads it. Reading every candidate would
   pull ~140 GB back onto a machine whose space was just reclaimed. Every file's
   status is checked before it is touched, and a placeholder is counted as
   "could not check", never assumed to be anything.

2. **Full hash of every byte, no shortcut.** An earlier attempt fingerprinted
   files by their first and last 64 KB. Audited against full hashes it produced
   a false "identical" -- so it is not used here. Slower and right beats faster
   and wrong when the output is a deletion list.

Size is a free pre-filter: two files cannot be identical unless their byte
counts match, so only files sharing a size with a DIFFERENTLY-named file are
ever read.

Writes system/data/content_duplicates.json, which `find_duplicates.py` picks up
and merges into the "Confirmed duplicates" sheet of the shared workbook, marked
"different names, same contents".
NOTHING IS DELETED, MOVED OR COPIED.
"""

import collections
import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

INDEX_DB = PROJECT_ROOT / "data" / "corpus_index.db"
OUT_JSON = PROJECT_ROOT / "data" / "content_duplicates.json"

RECALL_ON_DATA_ACCESS = 0x00400000
OFFLINE = 0x00001000
_SEP = chr(92)
_LONG = _SEP * 2 + "?" + _SEP

# Below this, a shared byte count is coincidence rather than a signal, and the
# space at stake is nil. Tiny files also dominate the candidate list, so they
# would soak up the whole time budget for nothing.
MIN_SIZE = 4096

_ga = ctypes.windll.kernel32.GetFileAttributesW
_ga.restype = ctypes.c_uint32


def _on_disk(path):
    """True only when the bytes are already here. A placeholder is False."""
    a = _ga(ctypes.c_wchar_p(_LONG + path))
    if a == 0xFFFFFFFF:
        return False
    return not (a & (RECALL_ON_DATA_ACCESS | OFFLINE))


def _sha256(path):
    h = hashlib.sha256()
    with open(_LONG + path, "rb") as fh:
        for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _candidates(root):
    """Files sharing a byte count with a DIFFERENTLY-named file, still on disk."""
    con = sqlite3.connect("file:{}?mode=ro".format(INDEX_DB), uri=True)
    rows = con.execute("SELECT path, name, size FROM files WHERE size >= ?",
                       (MIN_SIZE,)).fetchall()
    con.close()

    names_by_size = collections.defaultdict(set)
    for _p, name, size in rows:
        names_by_size[size].add(name)
    interesting = {s for s, names in names_by_size.items() if len(names) > 1}

    by_size = collections.defaultdict(list)
    skipped_cloud = 0
    for rel, name, size in rows:
        if size not in interesting:
            continue
        full = os.path.join(root, rel.replace("/", os.sep).replace(_SEP, os.sep))
        if not _on_disk(full):
            skipped_cloud += 1
            continue
        by_size[size].append((name, rel.replace(_SEP, "/"), full))

    # only worth hashing where two DIFFERENT names of that size are both here
    usable = {s: v for s, v in by_size.items() if len({n for n, _, _ in v}) > 1}
    return usable, skipped_cloud


def main():
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 1800.0
    root = str(config.CORPUS_DIR)

    print("Working out which files are worth reading...")
    usable, skipped_cloud = _candidates(root)
    total_files = sum(len(v) for v in usable.values())
    total_bytes = sum(s * len(v) for s, v in usable.items())
    print("  %d files in %d size-groups, %.1f GB to read" % (
        total_files, len(usable), total_bytes / 1e9))
    print("  %d candidates skipped -- they are cloud-only and would have to download"
          % skipped_cloud)
    print()

    # biggest first, so a time-limited run still covers what matters
    order = sorted(usable.items(), key=lambda kv: -kv[0] * len(kv[1]))

    found = []
    read_files = 0
    read_bytes = 0
    t0 = time.time()
    stopped_early = False

    for size, entries in order:
        if time.time() - t0 > budget:
            stopped_early = True
            break
        by_hash = collections.defaultdict(list)
        for name, rel, full in entries:
            try:
                by_hash[_sha256(full)].append((name, rel))
            except (OSError, PermissionError):
                continue
            read_files += 1
            read_bytes += size
        for digest, members in by_hash.items():
            distinct_names = {n for n, _ in members}
            if len(members) > 1 and len(distinct_names) > 1:
                found.append({
                    "sha256": digest,
                    "size": size,
                    "copies": len(members),
                    "distinct_names": sorted(distinct_names),
                    "paths": sorted(r for _, r in members),
                    "wasted": size * (len(members) - 1),
                })

    found.sort(key=lambda r: -r["wasted"])
    wasted = sum(r["wasted"] for r in found)

    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "method": "full SHA-256 of every byte; only files already on this disk",
        "files_read": read_files,
        "bytes_read": read_bytes,
        "groups_examined": len(order) if not stopped_early else "partial",
        "skipped_cloud_only": skipped_cloud,
        "stopped_early": stopped_early,
        "groups": found,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    print("Read %d files (%.1f GB) in %.0fs%s" % (
        read_files, read_bytes / 1e9, time.time() - t0,
        " -- STOPPED AT TIME LIMIT" if stopped_early else ""))
    print()
    print("SAME FILE, DIFFERENT NAME: %d groups, %.1f GB in redundant copies"
          % (len(found), wasted / 1e9))
    print()
    for r in found[:15]:
        print("  %.1f MB x %d copies -- %.1f MB wasted" % (
            r["size"] / 1e6, r["copies"], r["wasted"] / 1e6))
        for n in r["distinct_names"][:4]:
            print("       %s" % n[:96])
        print()
    print("Saved to %s" % OUT_JSON)
    print("Re-run find_duplicates.py to add it to the shared spreadsheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
