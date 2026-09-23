"""
verify_duplicates.py
--------------------
Prove -- or disprove -- every duplicate group in the report by comparing the
actual contents.

    python system/scripts/verify_duplicates.py [seconds]

WHY THIS EXISTS. `find_duplicates.py` groups files by name plus exact size.
That is free and it is a strong signal, but it is not proof. Measured on this
library it is wrong about 5% of the time. One measured case: a due diligence
appendix and its reissue years later carry the same name AND the same byte
count, and are different documents. A deletion list that
cannot tell those apart is dangerous.

FULL HASH, NO SHORTCUT. An earlier attempt fingerprinted each file by its first
and last 64 KB. Audited against full hashes it declared two different files
identical, so it was thrown away. Every byte is read here.

ONLY FILES ALREADY ON THIS DISK. The library is mostly cloud placeholders and
opening one downloads it. Reading everything would pull back the space the
machine just reclaimed. Each file's state is checked before it is touched.

FOUR VERDICTS, and the last two matter as much as the first:
  * identical            -- every copy that could be read is byte-for-byte equal
  * NOT IDENTICAL        -- copies differ; do not treat these as duplicates
  * partly checked       -- the readable copies match, others are cloud-only
  * not checked          -- fewer than two copies are on this disk

"Not checked" is never reported as "fine". Writes data/verified_duplicates.json,
which find_duplicates.py turns into a verdict column in the shared spreadsheet.
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
OUT_JSON = PROJECT_ROOT / "data" / "verified_duplicates.json"

RECALL_ON_DATA_ACCESS = 0x00400000
OFFLINE = 0x00001000
_SEP = chr(92)
_LONG = _SEP * 2 + "?" + _SEP

_ga = ctypes.windll.kernel32.GetFileAttributesW
_ga.restype = ctypes.c_uint32


def _on_disk(path):
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


def main():
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 2400.0
    root = str(config.CORPUS_DIR)

    con = sqlite3.connect("file:{}?mode=ro".format(INDEX_DB), uri=True)
    groups = collections.defaultdict(list)
    for name, size, path in con.execute(
            "SELECT name, size, path FROM files WHERE size > 0"):
        groups[(name, size)].append(path.replace(_SEP, "/"))
    con.close()

    dup = [(k, v) for k, v in groups.items()
           if len(v) > 1 and not k[0].lower().endswith(".msg")]
    # biggest space saving first, so a capped run covers what anyone would act on
    dup.sort(key=lambda kv: -kv[0][1] * (len(kv[1]) - 1))
    print("%d duplicate groups to check" % len(dup))

    results = {}
    counts = collections.Counter()
    bytes_by_verdict = collections.Counter()
    read_files = read_bytes = 0
    mismatches = []
    t0 = time.time()
    stopped_early = False

    for (name, size), paths in dup:
        if time.time() - t0 > budget:
            stopped_early = True
            break
        wasted = size * (len(paths) - 1)
        local = [p for p in paths
                 if _on_disk(os.path.join(root, p.replace("/", os.sep)))]

        if len(local) < 2:
            verdict = "not checked - copies are cloud-only"
        else:
            digests = {}
            failed = False
            for p in local:
                try:
                    digests.setdefault(
                        _sha256(os.path.join(root, p.replace("/", os.sep))), []).append(p)
                except (OSError, PermissionError):
                    failed = True
                    break
                read_files += 1
                read_bytes += size
            if failed:
                verdict = "not checked - could not read"
            elif len(digests) > 1:
                verdict = "NOT IDENTICAL - %d different versions" % len(digests)
                if len(mismatches) < 40:
                    mismatches.append({
                        "name": name, "size": size, "copies": len(paths),
                        "versions": len(digests),
                        "examples": [v[0] for v in digests.values()][:4],
                    })
            elif len(local) == len(paths):
                verdict = "identical - all %d copies compared" % len(paths)
            else:
                verdict = "identical so far - %d of %d compared, rest cloud-only" % (
                    len(local), len(paths))

        results["%s\t%d" % (name, size)] = verdict
        head = verdict.split(" -")[0]
        counts[head] += 1
        bytes_by_verdict[head] += wasted

    payload = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "method": "full SHA-256 of every byte; only files already on this disk",
        "groups_checked": len(results),
        "groups_total": len(dup),
        "files_read": read_files,
        "bytes_read": read_bytes,
        "stopped_early": stopped_early,
        "verdicts": results,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload), encoding="utf-8")

    print()
    print("Read %d files (%.1f GB) in %.0fs%s" % (
        read_files, read_bytes / 1e9, time.time() - t0,
        "  -- STOPPED AT TIME LIMIT" if stopped_early else ""))
    print()
    print("%-42s %8s %12s" % ("verdict", "groups", "space"))
    print("-" * 66)
    for v, n in counts.most_common():
        print("%-42s %8d %9.1f GB" % (v, n, bytes_by_verdict[v] / 1e9))

    checked = counts["identical"] + counts["identical so far"] + counts["NOT IDENTICAL"]
    if checked:
        print()
        print("  ==> of %d groups actually compared, %.2f%% were NOT identical"
              % (checked, 100.0 * counts["NOT IDENTICAL"] / checked))

    if mismatches:
        print()
        print("FILES THAT ARE NOT ACTUALLY DUPLICATES (same name, same size, different contents)")
        print("-" * 78)
        for m in mismatches[:12]:
            print("  %s  (%.1f MB, %d copies, %d versions)" % (
                m["name"][:56], m["size"] / 1e6, m["copies"], m["versions"]))
            for e in m["examples"][:2]:
                print("        %s" % e[:112])
            print()

    print("Saved to %s" % OUT_JSON)
    print("Re-run find_duplicates.py to put the verdicts in the spreadsheet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
