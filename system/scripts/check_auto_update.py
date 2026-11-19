"""
scripts/check_auto_update.py
-----------------------------
Checks for the automatic update install (scripts/apply_update.py::auto_apply and
its helpers). Run after any change to apply_update.py:

    .venv\\Scripts\\python.exe system/scripts/check_auto_update.py

Hand-rolled like check_portfolio_comparison.py: prints PASS/FAIL, exits non-zero
on any failure. Every check builds a throwaway `system/` tree in a temp folder;
nothing here touches the team folder, this machine's real pending update, logs
or backups.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

SCRIPTS = Path(__file__).parent
PROJECT_ROOT = SCRIPTS.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS))

import logging  # noqa: E402
logging.getLogger("vaulter.update").addHandler(logging.NullHandler())  # keep expected [ERROR] lines off the console

import apply_update as au  # noqa: E402

RESULTS = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, condition, detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""))


def has(name):
    return hasattr(au, name)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def snapshot(root: Path) -> dict:
    """relative path -> sha256 of every file outside data/."""
    return {
        str(p.relative_to(root)): sha(p)
        for p in root.rglob("*")
        if p.is_file() and "data" not in p.relative_to(root).parts
    }


def make_tree(tmp: Path, new_requirements: bytes = b"pkg==1\n") -> Path:
    """A temp `system/` at version 1.0 with a staged update to 2.0."""
    root = tmp / "system"
    (root / "sub").mkdir(parents=True)
    (root / "data" / "pending_update").mkdir(parents=True)
    (root / "VERSION").write_text("1.0\n")
    (root / "requirements.txt").write_bytes(b"pkg==1\n")
    (root / "a.py").write_text("old a\n")
    (root / "sub" / "b.py").write_text("old b\n")
    (root / "gone.py").write_text("removed upstream\n")
    zpath = root / "data" / "pending_update" / "u.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("VERSION", "2.0\n")
        zf.writestr("requirements.txt", new_requirements)
        zf.writestr("a.py", "new a\n")
        zf.writestr("sub/b.py", "old b\n")
        zf.writestr("c.py", "brand new\n")
    (root / "data" / "pending_update" / "ready.json").write_text(
        json.dumps({"version": "2.0", "zip_filename": "u.zip"}))
    return root


class Stub:
    """Replaces apply_update.apply_pending_update for the length of a with-block."""

    def __init__(self, fn):
        self.fn, self.calls = fn, 0

    def __enter__(self):
        self.orig = au.apply_pending_update

        def wrapper(project_root=None):
            self.calls += 1
            return self.fn(project_root)
        au.apply_pending_update = wrapper
        return self

    def __exit__(self, *exc):
        au.apply_pending_update = self.orig


def ok_apply(project_root):
    (project_root / "a.py").write_text("new a\n")
    return {"applied": True, "version": "2.0"}


print("\n1. The automatic install")

# --- helpers exist ---------------------------------------------------------
NEEDED = ("is_development_copy", "running_version", "staged_update_applicable",
          "release_changes_dependencies", "auto_apply", "recover_unfinished_apply",
          "other_servers_running", "_count_servers", "_sync_tree")
for fn in NEEDED:
    check(f"apply_update.{fn} exists", has(fn))

if not all(has(f) for f in NEEDED):
    print("\nFunctions missing; stopping here.")
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1)

# --- development copy ------------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    check("a normal install is not a development copy", au.is_development_copy(root) is False)
    (Path(t) / ".git").write_text("gitdir: elsewhere")
    check("a .git file beside system/ is a development copy", au.is_development_copy(root) is True)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (Path(t) / ".git").mkdir()
    check("a .git folder beside system/ is a development copy", au.is_development_copy(root) is True)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (root / "VERSION").unlink()
    check("no VERSION file is a development copy", au.is_development_copy(root) is True)
    check("running_version is empty with no VERSION", au.running_version(root) == "")
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    check("running_version reads the first line", au.running_version(root) == "1.0")

# --- staged_update_applicable ----------------------------------------------
sua = au.staged_update_applicable
check("a newer staged version is applicable", sua({"version": "b"}, "a") == "b")
check("the same version is not", sua({"version": "b"}, "b") == "")
check("a stage made on another version is superseded",
      sua({"version": "b", "current_version_at_download": "x"}, "a") == "")
check("a stage made on THIS version is applicable",
      sua({"version": "b", "current_version_at_download": "a"}, "a") == "b")
check("an unknown running version does not supersede",
      sua({"version": "b", "current_version_at_download": "x"}, "") == "b")
check("a non-dict marker is not applicable", sua([1], "a") == "")
check("None is not applicable", sua(None, "a") == "")
check("a marker with no version is not applicable", sua({"zip_filename": "z"}, "a") == "")

# --- auto_apply: dev copy ---------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (Path(t) / ".git").write_text("gitdir: elsewhere")
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("dev copy: not applied", r["applied"] is False)
    check("dev copy: reason names it", "development copy" in r["reason"], r["reason"])
    check("dev copy: apply never called", s.calls == 0)
    check("dev copy: files untouched", (root / "a.py").read_text() == "old a\n")

# --- auto_apply: nothing staged ---------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (root / "data" / "pending_update" / "ready.json").unlink()
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("nothing staged: not applied, apply never called",
          r["applied"] is False and s.calls == 0, r["reason"])

# --- auto_apply: happy path --------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("happy path: applied", r["applied"] is True and r["version"] == "2.0"
          and r["restored"] is False, str(r))
    check("happy path: apply called once", s.calls == 1)
    check("happy path: backup removed afterwards",
          not (root / "data" / "backups" / "pre_update_2.0").exists())
    check("happy path: lock released", not (root / "data" / "apply.lock").exists())

# --- at_startup and dependency changes -------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t), new_requirements=b"pkg==2\n")
    check("release_changes_dependencies sees a differing requirements.txt",
          au.release_changes_dependencies(root, root / "data" / "pending_update" / "u.zip") is True)
    with Stub(ok_apply) as s:
        r = au.auto_apply(root, at_startup=True)
    check("startup + changed dependencies: refused, says restart",
          r["applied"] is False and "restart" in r["reason"], r["reason"])
    check("startup + changed dependencies: apply never called", s.calls == 0)
    with Stub(ok_apply) as s:
        r = au.auto_apply(root, at_startup=False)
    check("not at startup + changed dependencies: applied", r["applied"] is True, str(r))
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    check("release_changes_dependencies is False when identical",
          au.release_changes_dependencies(root, root / "data" / "pending_update" / "u.zip") is False)
    check("release_changes_dependencies is True for an unreadable zip",
          au.release_changes_dependencies(root, root / "nope.zip") is True)
    orig = au.other_servers_running
    au.other_servers_running = lambda pr: True
    try:
        with Stub(ok_apply) as s:
            r = au.auto_apply(root, at_startup=True)
    finally:
        au.other_servers_running = orig
    check("startup + another server running: refused, says restart",
          r["applied"] is False and "restart" in r["reason"] and s.calls == 0, r["reason"])

# --- lock --------------------------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    lock = root / "data" / "apply.lock"
    lock.write_text("held")
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("fresh lock held: refused", r["applied"] is False and s.calls == 0, r["reason"])
    check("fresh lock held: lock left alone", lock.exists() and lock.read_text() == "held")
    old = time.time() - 601
    os.utime(lock, (old, old))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("lock 601 s old is stale: applied", r["applied"] is True, str(r))
    check("stale lock: released afterwards", not lock.exists())

# --- failure restores everything ---------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)

    def breaks(project_root):
        (project_root / "a.py").write_text("half written\n")
        (project_root / "gone.py").unlink()
        (project_root / "brand_new.py").write_text("left behind\n")
        raise RuntimeError("boom")

    with Stub(breaks):
        r = au.auto_apply(root)
    after = snapshot(root)
    check("failure: restored True, not applied",
          r["restored"] is True and r["applied"] is False, str(r))
    check("failure: every original file identical (sha256)", after == before,
          f"differs: {sorted(set(before.items()) ^ set(after.items()))[:4]}")
    check("failure: the new file is gone", not (root / "brand_new.py").exists())
    check("failure: lock released", not (root / "data" / "apply.lock").exists())

# --- real apply_update through _sync_tree, then restore ----------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    backup = Path(t) / "backup"
    for rel in before:
        (backup / rel).parent.mkdir(parents=True, exist_ok=True)
        (backup / rel).write_bytes((root / rel).read_bytes())
    up, dele = au.apply_update(root, root / "data" / "pending_update" / "u.zip")
    check("apply_update still syncs: new/changed in, removed out",
          (root / "c.py").exists() and not (root / "gone.py").exists()
          and (root / "a.py").read_text() == "new a\n" and (up, dele) == (5, 1), f"{up},{dele}")
    au._sync_tree(root, backup)
    check("_sync_tree restores a backup exactly", snapshot(root) == before)
    check("_sync_tree leaves data/ alone", (root / "data" / "pending_update" / "u.zip").exists())

# --- recover_unfinished_apply -------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    backup = root / "data" / "backups" / "pre_update_2.0"
    au._backup_tree(root, backup, au._marker_path(root, "2.0"), "1.0")
    marker = root / "data" / "apply_in_progress.json"
    check("recover: nothing to do without a marker", au.recover_unfinished_apply(root) is False)
    marker.write_text(json.dumps({"version": "2.0", "started": "x"}))
    (root / "a.py").write_text("half written\n")
    (root / "stray.py").write_text("stray\n")
    check("recover: restores and says so", au.recover_unfinished_apply(root) is True)
    check("recover: files identical to before (sha256)", snapshot(root) == before)
    check("recover: marker left for the reporter", marker.exists())
    check("recover: a second run does nothing", au.recover_unfinished_apply(root) is False)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    check("recover: marker but no backup restores nothing",
          au.recover_unfinished_apply(root) is False)

# --- a bad backup is never restored --------------------------------------------
import shutil  # noqa: E402
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    real_copy2, calls = shutil.copy2, [0]

    def flaky_copy2(src, dst, *a, **k):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("disk full")
        return real_copy2(src, dst, *a, **k)

    shutil.copy2 = flaky_copy2
    try:
        with Stub(ok_apply) as s:
            r = au.auto_apply(root)
    finally:
        shutil.copy2 = real_copy2
    check("failing backup: nothing applied, nothing restored, apply never called",
          r["applied"] is False and r["restored"] is False and s.calls == 0, str(r))
    check("failing backup: every original file untouched (sha256)", snapshot(root) == before)
    check("failing backup: partial backup removed, lock released",
          not (root / "data" / "backups" / "pre_update_2.0").exists()
          and not (root / "data" / "apply.lock").exists())
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    backup = root / "data" / "backups" / "pre_update_2.0"
    (backup).mkdir(parents=True)
    (backup / "a.py").write_text("old a")            # partial, no completion marker
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    check("recover: a backup without its completion marker is refused",
          au.recover_unfinished_apply(root) is False and snapshot(root) == before)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    backup = root / "data" / "backups" / "pre_update_2.0"
    au._backup_tree(root, backup, au._marker_path(root, "2.0"), "0.9")   # made from another version
    before = snapshot(root)
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    check("recover: a backup made from a different version is refused",
          au.recover_unfinished_apply(root) is False and snapshot(root) == before)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))

    def breaks2(project_root):
        raise RuntimeError("boom")

    with Stub(breaks2):
        r = au.auto_apply(root)
    check("failed install: backup and marker deleted after the restore",
          r["restored"] is True and not (root / "data" / "backups" / "pre_update_2.0").exists()
          and not au._marker_path(root, "2.0").exists())

# --- counting servers ----------------------------------------------------------
X = Path("C:/x/system/main.py")
CX = r"python C:\x\system\main.py mcp"
CY = r"python C:\y\system\main.py mcp"
n = au._count_servers([(10, 1, CX), (11, 10, CX)], X, self_pid=11)
check("_count_servers: our own launcher + child count as zero others", n == 0, str(n))
n = au._count_servers([(10, 1, CX), (11, 10, CX), (20, 1, CX), (21, 20, CX)], X, self_pid=11)
check("_count_servers: another launcher + child counts once", n == 1, str(n))
n = au._count_servers([(10, 1, CX), (11, 10, CX), (20, 1, CY), (21, 20, CY)], X, self_pid=11)
check("_count_servers: a different install's pair is not counted", n == 0, str(n))
n = au._count_servers([(1, 5, CX), (2, 6, CX)], X, self_pid=1)
check("_count_servers: single processes without launchers, one other", n == 1, str(n))
n = au._count_servers([(2, 1, r"python C:\x\system\main.py search foo")], X, self_pid=1)
check("_count_servers ignores non-mcp commands", n == 0, str(n))
n = au._count_servers([(10, 1, CX), (11, 10, CX),
                       (12, 11, r"python apply_update.py --auto --startup")],
                      X, self_pid=12)
check("_count_servers: the startup CLI under its own server counts zero others", n == 0, str(n))
n = au._count_servers([(10, 1, CX), (11, 10, CX),
                       (12, 11, r"python apply_update.py --auto --startup"),
                       (20, 1, CX), (21, 20, CX)], X, self_pid=12)
check("_count_servers: the startup CLI plus one more server counts one", n == 1, str(n))
check("other_servers_running returns a bool on a real tree",
      isinstance(au.other_servers_running(PROJECT_ROOT), bool))

# --- derived paths match config -----------------------------------------------
import config  # noqa: E402
check("project_root-derived paths equal config's",
      PROJECT_ROOT / "data" / "pending_update" == config.PENDING_UPDATE_DIR
      and PROJECT_ROOT / "data" / "apply_in_progress.json" == config.APPLY_IN_PROGRESS_FILE
      and PROJECT_ROOT / "data" == config.DATA_DIR)

failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
