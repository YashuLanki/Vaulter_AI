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

# The post-install smoke test imports mcp_server in a child process. Every check
# here gets a stub that passes, unless it sets its own; the real one is checked
# on its own against throwaway trees in section 1b.
_REAL_SMOKE = getattr(au, "_smoke_test", None)
_SMOKE_CALLS = []


def _passing_smoke(project_root, timeout=120):
    _SMOKE_CALLS.append((project_root, timeout))
    return True


au._smoke_test = _passing_smoke

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
    (root / "main.py").write_text("old main\n")
    zpath = root / "data" / "pending_update" / "u.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("VERSION", "2.0\n")
        zf.writestr("main.py", "new main\n")
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
        self.fn, self.calls, self.kwargs = fn, 0, []

    def __enter__(self):
        self.orig = au.apply_pending_update

        def wrapper(project_root=None, **kw):
            self.calls += 1
            self.kwargs.append(kw)
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
    old = time.time() - 899
    os.utime(lock, (old, old))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("lock 899 s old is still fresh: refused", r["applied"] is False and s.calls == 0, str(r))
    lock.write_text("held")
    old = time.time() - 901
    os.utime(lock, (old, old))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("lock 901 s old is stale: applied", r["applied"] is True, str(r))
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
          and (root / "a.py").read_text() == "new a\n" and (up, dele) == (6, 1), f"{up},{dele}")
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

# --- recovery of a half-applied tree whose VERSION was already copied -------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    au._backup_tree(root, root / "data" / "backups" / "pre_update_2.0",
                    au._marker_path(root, "2.0"), "1.0")
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    (root / "VERSION").write_text("2.0")
    (root / "a.py").write_text("half written")
    (root / "sub" / "b.py").unlink()
    check("recover: a tree already on the target VERSION is still rescued",
          au.recover_unfinished_apply(root) is True)
    check("recover: ...and every original file is sha256-identical", snapshot(root) == before)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    au._backup_tree(root, root / "data" / "backups" / "pre_update_2.0",
                    au._marker_path(root, "2.0"), "1.0")
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    (root / "VERSION").write_text("3.0")
    check("recover: a tree on some OTHER version is left alone",
          au.recover_unfinished_apply(root) is False and (root / "VERSION").read_text() == "3.0")
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    order = []
    real_copy2 = __import__("shutil").copy2
    __import__("shutil").copy2 = lambda src, dst, *a, **k: (order.append(Path(dst).name), real_copy2(src, dst, *a, **k))[1]
    try:
        au.apply_update(root, root / "data" / "pending_update" / "u.zip")
    finally:
        __import__("shutil").copy2 = real_copy2
    check("_sync_tree copies VERSION last", order and order[-1] == "VERSION", str(order))

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

# =============================================================================
print("\n1b. Installer safety (final review: C1, C2, I1-I3, I5c, I6, M5, T1)")
import contextlib  # noqa: E402
import inspect  # noqa: E402
import io  # noqa: E402
import types  # noqa: E402


class Capture(logging.Handler):
    """Collects records from the installer's logger for the length of a with-block."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        logging.getLogger("vaulter.update").addHandler(self)
        return self

    def __exit__(self, *exc):
        logging.getLogger("vaulter.update").removeHandler(self)

    def errors(self):
        return [r.getMessage() for r in self.records if r.levelno >= logging.ERROR]


def set_smoke(fn):
    au._smoke_test = fn


# --- M5: the lock outlives the slowest automatic install ---------------------------
check("M5: LOCK_STALE_SECONDS is 900", getattr(au, "LOCK_STALE_SECONDS", 0) == 900,
      str(getattr(au, "LOCK_STALE_SECONDS", None)))
check("M5: ...above the automatic pip limit", getattr(au, "LOCK_STALE_SECONDS", 0)
      > getattr(au, "AUTO_PIP_TIMEOUT", 10 ** 6))

# --- C1(b): pip limits -----------------------------------------------------------------
_rd = inspect.signature(au.refresh_dependencies).parameters
check("C1: refresh_dependencies takes a timeout, 600 by default",
      "timeout" in _rd and _rd["timeout"].default == 600)
_apu = inspect.signature(au.apply_pending_update).parameters
check("C1: apply_pending_update's pip limit defaults to 600 (the manual path)",
      "pip_timeout" in _apu and _apu["pip_timeout"].default == 600)
check("C1: AUTO_PIP_TIMEOUT is 300", getattr(au, "AUTO_PIP_TIMEOUT", None) == 300)
with tempfile.TemporaryDirectory() as t:
    (Path(t) / "requirements.txt").write_text("pkg==1\n")
    seen = {}
    real_run = au.subprocess.run

    def fake_pip(cmd, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=0)
    au.subprocess.run = fake_pip
    try:
        au.refresh_dependencies(Path(t), timeout=300) if "timeout" in _rd else None
    finally:
        au.subprocess.run = real_run
    check("C1: refresh_dependencies hands its timeout to pip", seen.get("timeout") == 300, str(seen))
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root)
    check("C1: the idle (automatic) path installs with a 300 s pip limit",
          r["applied"] is True and s.kwargs[:1] == [{"pip_timeout": 300}], str(s.kwargs))
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root, at_startup=True)
    check("C1: the startup path's pip limit fits inside main.py's 120 s",
          r["applied"] is True and s.kwargs and 0 < s.kwargs[0].get("pip_timeout", 999) <= 60,
          str(s.kwargs))

# --- C2: a failed dependency install is a failure ------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)

    def deps_fail(project_root):
        (project_root / "a.py").write_text("new a\n")
        (project_root / "requirements.txt").write_bytes(b"pkg==2\n")
        (project_root / "c.py").write_text("brand new\n")
        return {"applied": True, "version": "2.0", "dependencies_ok": False,
                "dependencies_message": "pip failed"}

    with Stub(deps_fail), Capture() as cap:
        r = au.auto_apply(root)
    check("C2: dependencies failed -> not applied, restored",
          r["applied"] is False and r["restored"] is True, str(r))
    check("C2: every original file sha256-identical (old requirements.txt back)",
          snapshot(root) == before, f"differs: {sorted(set(before.items()) ^ set(snapshot(root).items()))[:4]}")
    check("C2: exactly one [ERROR], naming the version", len(cap.errors()) == 1
          and "2.0" in cap.errors()[0], str(cap.errors()))
    check("C2: backup and lock gone afterwards",
          not (root / "data" / "backups" / "pre_update_2.0").exists()
          and not (root / "data" / "apply.lock").exists())

# --- I2: smoke test after the install, before the backup is discarded -----------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)
    seen_note = []

    def failing_smoke(project_root, timeout=120):
        note = project_root / "data" / "apply_in_progress.json"
        seen_note.append(json.loads(note.read_text()).get("version") if note.exists() else None)
        return False

    set_smoke(failing_smoke)
    try:
        with Stub(ok_apply), Capture() as cap:
            r = au.auto_apply(root)
    finally:
        set_smoke(_passing_smoke)
    check("I2: forced smoke failure -> not applied, restored",
          r["applied"] is False and r["restored"] is True, str(r))
    check("I2: ...every original file sha256-identical", snapshot(root) == before)
    check("I2: ...one [ERROR] naming only the version", len(cap.errors()) == 1
          and "2.0" in cap.errors()[0] and ".py" not in cap.errors()[0], str(cap.errors()))
    check("I2: the smoke test runs with the in-progress note in place", seen_note == ["2.0"], str(seen_note))
    check("I2: ...and the note is cleared after the restore",
          not (root / "data" / "apply_in_progress.json").exists())
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    _SMOKE_CALLS.clear()
    with Stub(ok_apply):
        r = au.auto_apply(root)
    check("I2: smoke passes -> applied, smoke run once with 120 s",
          r["applied"] is True and r["restored"] is False
          and [c[1] for c in _SMOKE_CALLS] == [120], str(_SMOKE_CALLS))
    check("I2: ...in-progress note cleared, backup gone",
          not (root / "data" / "apply_in_progress.json").exists()
          and not (root / "data" / "backups" / "pre_update_2.0").exists())
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    before = snapshot(root)

    def killed_smoke(project_root, timeout=120):
        raise SystemExit("killed from outside")

    set_smoke(killed_smoke)
    try:
        with Stub(ok_apply):
            try:
                au.auto_apply(root)
            except SystemExit:
                pass
    finally:
        set_smoke(_passing_smoke)
    check("I2: a process killed during the smoke test is put back at the next start",
          au.recover_unfinished_apply(root) is True and snapshot(root) == before)

# the real smoke test, on throwaway trees (never this install)
if _REAL_SMOKE is not None:
    with tempfile.TemporaryDirectory() as t:
        good = Path(t) / "good"
        good.mkdir()
        (good / "mcp_server.py").write_text("X = 1\n")
        bad = Path(t) / "bad"
        bad.mkdir()
        (bad / "mcp_server.py").write_text("raise ImportError('broken release')\n")
        slow = Path(t) / "slow"
        slow.mkdir()
        (slow / "mcp_server.py").write_text("import time\ntime.sleep(60)\n")
        check("I2: real smoke test passes on an importable tree", _REAL_SMOKE(good, 60) is True)
        check("I2: real smoke test fails on a tree whose import raises", _REAL_SMOKE(bad, 60) is False)
        t0 = time.monotonic()
        check("I2: real smoke test fails on a timeout, and stops waiting",
              _REAL_SMOKE(slow, 2) is False and time.monotonic() - t0 < 20)
else:
    check("I2: apply_update._smoke_test exists", False)
_smoke_src = inspect.getsource(_REAL_SMOKE) if _REAL_SMOKE else ""
check("I2: smoke test: stdin closed, output to a file, timeout, cwd the system folder",
      "stdin=subprocess.DEVNULL" in _smoke_src and "PIPE" not in _smoke_src
      and "timeout=timeout" in _smoke_src and "cwd=str(project_root)" in _smoke_src)

# --- I6: the staged zip must look like a release ---------------------------------------
for label, files, want in (
        ("no main.py at the top level", {"VERSION": "2.0\n", "sub/main.py": "x"}, False),
        ("VERSION does not match the staged version", {"VERSION": "9.9\n", "main.py": "x"}, False),
        ("no VERSION at all", {"main.py": "x"}, False)):
    with tempfile.TemporaryDirectory() as t:
        root = make_tree(Path(t))
        zpath = root / "data" / "pending_update" / "u.zip"
        zpath.unlink()
        with zipfile.ZipFile(zpath, "w") as zf:
            for name, text in files.items():
                zf.writestr(name, text)
        before = snapshot(root)
        with Stub(ok_apply) as s, Capture() as cap:
            r = au.auto_apply(root)
        check(f"I6: {label} -> refused, nothing called or changed",
              r["applied"] is False and s.calls == 0 and snapshot(root) == before, str(r))
        check(f"I6: {label} -> one [ERROR]", len(cap.errors()) == 1, str(cap.errors()))
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    with Stub(ok_apply) as s, Capture() as cap:
        r = au.auto_apply(root)
    check("I6: a good zip (main.py, matching VERSION) -> applied, no [ERROR]",
          r["applied"] is True and s.calls == 1 and cap.errors() == [], str(r))

# --- I1: a startup install is recorded, so no restart is owed for it --------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    rec = root / "data" / "startup_installed.json"
    with Stub(ok_apply):
        r = au.auto_apply(root, at_startup=True)
    check("I1: a successful startup install records its version",
          r["applied"] is True and rec.exists() and json.loads(rec.read_text()) == {"version": "2.0"},
          rec.read_text() if rec.exists() else "missing")
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    with Stub(ok_apply):
        au.auto_apply(root, at_startup=False)
    check("I1: an idle-path install records nothing", not (root / "data" / "startup_installed.json").exists())
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t), new_requirements=b"pkg==2\n")
    with Stub(ok_apply):
        au.auto_apply(root, at_startup=True)
    check("I1: a refused startup install records nothing",
          not (root / "data" / "startup_installed.json").exists())
_gi = PROJECT_ROOT.parent / ".gitignore"
if _gi.exists():          # a repo checkout; an installed copy has no .gitignore
    check("I1: startup_installed.json is gitignored",
          "system/data/startup_installed.json" in _gi.read_text(encoding="utf-8"))

# --- I5(c): a version the task gave up on may change dependencies at startup -------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t), new_requirements=b"pkg==2\n")
    (root / "data" / "auto_restart.json").write_text(json.dumps({"2.0": {"restarts": 2, "gave_up": True,
                                                                         "gave_up_at": 1.0}}))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root, at_startup=True)
    check("I5c: startup + changed dependencies + this version gave up -> applied",
          r["applied"] is True and s.calls == 1, str(r))
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t), new_requirements=b"pkg==2\n")
    (root / "data" / "auto_restart.json").write_text(json.dumps({"1.9": {"restarts": 2, "gave_up": True}}))
    with Stub(ok_apply) as s:
        r = au.auto_apply(root, at_startup=True)
    check("I5c: ...another version's give-up does not count",
          r["applied"] is False and s.calls == 0, str(r))

# --- I3: recovery takes the lock -------------------------------------------------------------
def half_applied(t):
    root = make_tree(Path(t))
    before = snapshot(root)
    au._backup_tree(root, root / "data" / "backups" / "pre_update_2.0", au._marker_path(root, "2.0"), "1.0")
    (root / "data" / "apply_in_progress.json").write_text(json.dumps({"version": "2.0"}))
    (root / "a.py").write_text("half written\n")
    return root, before


with tempfile.TemporaryDirectory() as t:
    root, before = half_applied(t)
    lock = root / "data" / "apply.lock"
    lock.write_text("held")
    mid = snapshot(root)
    check("I3: fresh lock held -> recovery returns False", au.recover_unfinished_apply(root) is False)
    check("I3: ...and touches nothing (tree, backup, lock)",
          snapshot(root) == mid and (root / "data" / "backups" / "pre_update_2.0").is_dir()
          and lock.read_text() == "held")
    old = time.time() - 901
    os.utime(lock, (old, old))
    check("I3: stale lock -> recovery proceeds", au.recover_unfinished_apply(root) is True
          and snapshot(root) == before)
    check("I3: ...and releases the lock it took", not lock.exists())

clock = {"t": 0.0}
sleeps = []


def fake_sleep(sec):
    sleeps.append(sec)
    clock["t"] += sec


def fake_clock():
    return clock["t"]


if hasattr(au, "_wait_for_lock"):
    with tempfile.TemporaryDirectory() as t:
        lock = Path(t) / "apply.lock"
        sleeps.clear()
        check("I3: no lock -> no wait", au._wait_for_lock(lock, sleep=fake_sleep, clock=fake_clock) is True
              and sleeps == [])
        lock.write_text("held")
        sleeps.clear()
        clock["t"] = 0.0
        ok = au._wait_for_lock(lock, sleep=fake_sleep, clock=fake_clock)
        check("I3: a lock that stays -> gives up after 60 s of wall time",
              ok is False and 59 <= clock["t"] <= 61, f"{clock['t']}")

        def clears_after_three(sec):
            fake_sleep(sec)
            if len(sleeps) == 3:
                lock.unlink()
        sleeps.clear()
        clock["t"] = 0.0
        ok = au._wait_for_lock(lock, sleep=clears_after_three, clock=fake_clock)
        check("I3: a lock that clears -> stops waiting at once", ok is True and len(sleeps) == 3)
else:
    check("I3: apply_update._wait_for_lock exists", False)
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (root / "data" / "apply.lock").write_text("held")
    sleeps.clear()
    clock["t"] = 0.0
    out = io.StringIO()
    try:
        with Stub(ok_apply) as s, contextlib.redirect_stdout(out):
            au._auto_cli(True, root, sleep=fake_sleep, clock=fake_clock)
        waited_ok = True
    except TypeError as e:
        waited_ok = False
    check("I3: --auto --startup waits for a fresh lock (about 60 s), then still prints one line",
          waited_ok and 59 <= clock["t"] <= 61 and len(out.getvalue().strip().splitlines()) == 1,
          f"{clock['t']} {out.getvalue()!r}")

# --- T1: data survives a failure restore -------------------------------------------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))
    (root / "confidentials").mkdir()
    (root / "confidentials" / ".env").write_text("SECRET=1\n")
    (root / ".venv" / "Lib").mkdir(parents=True)
    (root / ".venv" / "Lib" / "site.py").write_text("venv file\n")
    (root / "data" / "keep").mkdir()
    (root / "data" / "keep" / "notes.json").write_text('{"k": 1}')

    def kept(r):
        out = {}
        for sub in ("confidentials", ".venv", "data"):
            for p in (r / sub).rglob("*"):
                rel = p.relative_to(r)
                if p.is_file() and not ({"backups", "logs"} & set(rel.parts)) and p.name != "apply.lock":
                    out[str(rel)] = sha(p)
        return out
    before = kept(root)

    def breaks_t1(project_root):
        (project_root / "a.py").write_text("half\n")
        raise RuntimeError("boom")

    with Stub(breaks_t1):
        r = au.auto_apply(root)
    check("T1: confidentials/, .venv/ and data/ byte-identical after a failure restore",
          r["restored"] is True and kept(root) == before and len(before) >= 5,
          f"{len(before)} files; differs: {sorted(set(before.items()) ^ set(kept(root).items()))[:3]}")


# =============================================================================
print("\n2. Install at startup")
import ast  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import types  # noqa: E402

_main_src = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
_fn_node = next((n for n in ast.parse(_main_src).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_auto_install_at_startup"), None)
check("main.py defines _auto_install_at_startup", _fn_node is not None)

_calls = []


def _fake_run(cmd, **kw):
    _calls.append((cmd, kw))
    return types.SimpleNamespace(returncode=0)


def _sub(run):
    return types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT,
                                 TimeoutExpired=subprocess.TimeoutExpired)


_ns = {"subprocess": _sub(_fake_run), "sys": sys, "Path": Path}
if _fn_node is not None:
    exec(compile(ast.Module(body=[_fn_node], type_ignores=[]), "main.py", "exec"), _ns)
_fn = _ns.get("_auto_install_at_startup")

if _fn:
    with tempfile.TemporaryDirectory() as t:
        sd = Path(t) / "system"
        (sd / "data").mkdir(parents=True)
        _calls.clear()
        _fn(sd)
        check("startup: nothing staged -> subprocess never called", _calls == [])

        (sd / "data" / "pending_update").mkdir()
        (sd / "data" / "pending_update" / "ready.json").write_text("{}")
        _calls.clear()
        _fn(sd)
        c = _calls[0] if len(_calls) == 1 else None
        check("startup: ready.json -> called once", c is not None, str(len(_calls)))
        if c:
            cmd, kw = c
            check("startup: runs apply_update.py --auto --startup with this interpreter",
                  str(cmd[0]) == sys.executable and Path(str(cmd[1])) == sd / "scripts" / "apply_update.py"
                  and "--auto" in cmd and "--startup" in cmd, str(cmd))
            check("startup: stdin DEVNULL, no pipes, timeout 120, cwd is the system dir",
                  kw.get("stdin") == subprocess.DEVNULL and kw.get("timeout") == 120
                  and str(kw.get("cwd")) == str(sd) and "capture_output" not in kw
                  and kw.get("stdout") is not subprocess.PIPE, str(kw))
            check("startup: output goes to a file, data/logs/auto_install.txt",
                  (sd / "data" / "logs" / "auto_install.txt").exists())

        (sd / "data" / "pending_update" / "ready.json").unlink()
        (sd / "data" / "apply_in_progress.json").write_text("{}")
        _calls.clear()
        _fn(sd)
        check("startup: apply_in_progress.json alone also triggers it", len(_calls) == 1)

        def _boom(cmd, **kw):
            raise OSError("no such thing")
        _ns["subprocess"] = _sub(_boom)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                _fn(sd)
            raised = False
        except BaseException:
            raised = True
        check("startup: a failing subprocess.run does not propagate", not raised)
        check("startup: never writes to stdout", buf.getvalue() == "", repr(buf.getvalue()))

_call_at = _main_src.find("_auto_install_at_startup(Path(__file__).parent)")
check("main.py calls it before importing config",
      _call_at != -1 and _call_at < _main_src.find("from config import LOG_DIR"))
check("main.py calls it only for the mcp command",
      re.search(r'sys\.argv\[1\] == "mcp"[^\n]*:\s*\n\s*_auto_install_at_startup', _main_src) is not None)

# --- the CLI logs errors where the team's error reports look ---------------------
with tempfile.TemporaryDirectory() as t:
    root = make_tree(Path(t))

    def breaks2(project_root):
        raise RuntimeError("boom")

    out = io.StringIO()
    with Stub(breaks2), contextlib.redirect_stdout(out):
        au._auto_cli(False, root)
    logf = root / "data" / "logs" / "vaulter.log"
    text = logf.read_text(encoding="utf-8") if logf.exists() else ""
    check("CLI: failed install writes data/logs/vaulter.log", logf.exists())
    check("CLI: an [ERROR] line in the report format",
          re.search(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}  \[ERROR\]  .*automatic update", text, re.M)
          is not None, text[:200])
    check("CLI: no separate auto_update.log", not (root / "data" / "logs" / "auto_update.log").exists())
    check("CLI: still prints one line", len(out.getvalue().strip().splitlines()) == 1, out.getvalue())
    check("CLI: its file handler is removed afterwards",
          not any(isinstance(h, logging.FileHandler) for h in logging.getLogger().handlers))


# =============================================================================
print("\n3. Idle restart")
import inspect  # noqa: E402,F811
# Every external effect is a stub passed in `ops`: no check here lists, stops or
# starts a real process, reads real idle time or asks the real release channel.
try:
    import auto_restart as ar  # noqa: E402
except Exception as _e:  # noqa: BLE001
    ar = None
    check("auto_restart imports", False, f"{type(_e).__name__}: {_e}")

AR_NEEDED = ("desktop_root", "relaunch_command", "restart_owed", "may_restart",
             "record_restart", "idle_seconds", "main")
for fn in AR_NEEDED:
    check(f"auto_restart.{fn} exists", ar is not None and hasattr(ar, fn))

STORE_EXE = r"C:\Program Files\WindowsApps\Claude_6.6395.0.0_x64__pzs8sxrjxfjjc\app\claude.exe"
CLI_EXE = r"C:\Users\x\.local\bin\claude.exe"
CLASSIC_EXE = r"C:\Users\x\AppData\Local\AnthropicClaude\app-0.9.0\claude.exe"
DESKTOP_START = "2026-11-19T08:00:00.1234567-07:00"   # PowerShell's 'o' format, 7 digits
DESKTOP_START_EPOCH = 1795100400.123456               # the same instant, seconds since 1970 UTC


def row(pid, ppid, exe, created=DESKTOP_START, typ=None):
    """typ: the Electron helper type (--type=...); None for a main process."""
    return {"ProcessId": pid, "ParentProcessId": ppid, "ExecutablePath": exe,
            "CommandLine": f'"{exe}"' + (f" --type={typ}" if typ else ""), "CreationDate": created}


# Recorded on this machine 2026-11-19: Desktop parent 34004 (its parent 12308 is
# not Desktop) with eight children; four Claude Code CLI processes. Every child
# carries an Electron --type= (the live listing that day: crashpad-handler,
# gpu-process, three utility, three renderer); the main process carries none.
_HELPER_TYPES = ("crashpad-handler", "gpu-process", "utility", "renderer",
                 "renderer", "renderer", "utility", "utility")
DESKTOP_ROWS = [row(34004, 12308, STORE_EXE)] + [
    row(p, 34004, STORE_EXE, "2026-11-19T08:00:05.0000000-07:00", typ)
    for p, typ in zip((24540, 9492, 9420, 4912, 21040, 35000, 22996, 22764), _HELPER_TYPES)]
CLI_ROWS = [row(15912, 8356, CLI_EXE), row(18916, 7284, CLI_EXE),
            row(33016, 18292, CLI_EXE), row(18024, 33524, CLI_EXE)]
CLI_PIDS = {r["ProcessId"] for r in CLI_ROWS}
START_APPS = [{"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude"}]
STORE_CMD = ["explorer.exe", "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude"]


class Env:
    """A stubbed world for auto_restart.main. `calls` records every effect in order."""

    def __init__(self, procs=None, after_kill=None, idle=1000.0, start_apps=None,
                 apply_result=None, relist=None, list_cost=0.0, recover_raises=False,
                 apply_raises=False, relaunch_ok=True, now=DESKTOP_START_EPOCH + 3600):
        self.procs = list(DESKTOP_ROWS + CLI_ROWS) if procs is None else procs
        self.after_kill = list(CLI_ROWS) if after_kill is None else after_kill
        self.relist = relist              # what the re-check right before taskkill sees
        self.list_cost = list_cost        # seconds of (fake) wall time each listing takes
        self.recover_raises, self.apply_raises = recover_raises, apply_raises
        self.relaunch_ok = relaunch_ok
        self.clock = 0.0                  # fake monotonic clock
        self.now = now                    # fake epoch time
        self.n_list = 0
        self.killed = False
        self.idle = idle
        self.apps = START_APPS if start_apps is None else start_apps
        self.apply_result = apply_result or {"applied": True, "version": "2.0",
                                             "reason": "", "restored": False}
        self.calls = []

    def ops(self):
        return {
            "list_processes": self.list_processes, "start_apps": self.start_apps,
            "taskkill": self.taskkill, "relaunch": self.relaunch,
            "idle_seconds": self.idle_seconds, "channel_check": self.channel_check,
            "recover": self.recover, "auto_apply": self.auto_apply, "sleep": self.sleep,
            "monotonic": self.monotonic, "time": self.time,
        }

    def names(self):
        return [c[0] for c in self.calls]

    def list_processes(self):
        self.calls.append(("list_processes",))
        self.n_list += 1
        self.clock += self.list_cost
        if self.n_list == 2 and self.relist is not None and not self.killed:
            return list(self.relist)
        return list(self.after_kill if self.killed else self.procs)

    def monotonic(self):
        return self.clock

    def time(self):
        return self.now

    def start_apps(self):
        self.calls.append(("start_apps",))
        return self.apps

    def taskkill(self, pid):
        self.calls.append(("taskkill", pid))
        self.killed = True
        return 0

    def relaunch(self, cmd):
        self.calls.append(("relaunch", list(cmd)))
        return self.relaunch_ok

    def idle_seconds(self):
        self.calls.append(("idle_seconds",))
        return self.idle

    def channel_check(self):
        self.calls.append(("channel_check",))

    def recover(self, project_root):
        self.calls.append(("recover", project_root))
        if self.recover_raises:
            raise RuntimeError("recover broke")
        return False

    def auto_apply(self, project_root, at_startup=False):
        self.calls.append(("auto_apply", project_root, at_startup))
        if self.apply_raises:
            raise RuntimeError("install broke")
        return dict(self.apply_result)

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))
        self.clock += seconds


def restart_tree(tmp: Path, dev: bool = False, staged: bool = True) -> Path:
    """A temp `system/` on 1.0 with 2.0 staged (made while 1.0 ran); VERSION
    written well before Desktop started, so only the stage makes a restart owed."""
    root = tmp / "system"
    (root / "data" / "pending_update").mkdir(parents=True)
    (root / "VERSION").write_text("1.0\n")
    old = DESKTOP_START_EPOCH - 86400
    os.utime(root / "VERSION", (old, old))
    if staged:
        (root / "data" / "pending_update" / "ready.json").write_text(json.dumps(
            {"version": "2.0", "zip_filename": "u.zip", "current_version_at_download": "1.0"}))
    if dev:
        (tmp / ".git").write_text("gitdir: elsewhere")
    return root


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def state_of(root: Path):
    p = root / "data" / "auto_restart.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


RLOG = Path("data") / "logs" / "auto_restart.txt"
VLOG = Path("data") / "logs" / "vaulter.log"
ERROR_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}  \[ERROR\]  .+$", re.M)
_real_state_before = (PROJECT_ROOT / "data" / "auto_restart.json").exists()
_real_rlog_before = read(PROJECT_ROOT / RLOG)

if ar is not None and all(hasattr(ar, f) for f in AR_NEEDED):
    # --- desktop_root ---------------------------------------------------------
    r = ar.desktop_root(CLI_ROWS + DESKTOP_ROWS)
    check("desktop_root picks the WindowsApps parent (34004)",
          r is not None and r["ProcessId"] == 34004, str(r and r["ProcessId"]))
    check("desktop_root: only Claude Code CLI processes -> None", ar.desktop_root(CLI_ROWS) is None)
    check("desktop_root: no processes -> None", ar.desktop_root([]) is None)
    tricky = [row(50, 1, r"C:\Users\x\.local\bin\AnthropicClaude\claude.exe"),
              row(51, 1, r"C:\Users\x\.local\bin\WindowsApps\Claude_1\claude.exe")]
    check("desktop_root: never a .local\\bin path, even one naming Desktop's folders",
          ar.desktop_root(tricky) is None)
    under = DESKTOP_ROWS + [row(60, 34004, CLI_EXE)]
    r = ar.desktop_root(under)
    check("desktop_root: a Claude Code child of Desktop does not change the root",
          r is not None and r["ProcessId"] == 34004)
    classic = [row(70, 9, CLASSIC_EXE), row(71, 70, CLASSIC_EXE, typ="renderer")] + CLI_ROWS
    r = ar.desktop_root(classic)
    check("desktop_root: a classic AnthropicClaude install's parent",
          r is not None and r["ProcessId"] == 70)
    lower = [dict(row(80, 9, STORE_EXE.lower().replace("\\", "/")))]
    check("desktop_root: path match is case- and slash-insensitive",
          (ar.desktop_root(lower) or {}).get("ProcessId") == 80)
    check("desktop_root: a row with no ExecutablePath is skipped",
          ar.desktop_root([{"ProcessId": 5, "ParentProcessId": 1, "ExecutablePath": None,
                            "CommandLine": None, "CreationDate": ""}]) is None)

    # --- relaunch_command ------------------------------------------------------
    rc = ar.relaunch_command
    check("relaunch_command: Store install -> explorer shell:AppsFolder\\<AppID>",
          rc(STORE_EXE, START_APPS) == STORE_CMD, str(rc(STORE_EXE, START_APPS)))
    check("relaunch_command: classic install -> its own executable",
          rc(CLASSIC_EXE, []) == [CLASSIC_EXE])
    check("relaunch_command: Store install with no Start entry -> []", rc(STORE_EXE, []) == [])
    check("relaunch_command: an entry for another package family is not used",
          rc(STORE_EXE, [{"Name": "Claude", "AppID": "Claude_otherpub!Claude"}]) == [])
    check("relaunch_command: a name that is not exactly Claude is not used",
          rc(STORE_EXE, [{"Name": "Claude Code", "AppID": "Claude_pzs8sxrjxfjjc!Claude"}]) == [])
    check("relaunch_command: the right entry is found among others",
          rc(STORE_EXE, [{"Name": "Notepad", "AppID": "x!y"}] + START_APPS) == STORE_CMD)
    check("relaunch_command: an unknown path -> []", rc(CLI_EXE, START_APPS) == [])
    check("relaunch_command: an empty path -> []", rc("", START_APPS) == [])

    # --- restart_owed ------------------------------------------------------------
    staged = {"version": "2.0", "current_version_at_download": "1.0"}
    ro = ar.restart_owed
    check("restart_owed: an applicable staged update is owed",
          ro(staged, "1.0", 100.0, 200.0) == "2.0")
    check("restart_owed: VERSION changed after Desktop started (applied by hand) -> running",
          ro(None, "2.0", 300.0, 200.0) == "2.0")
    check("restart_owed: neither -> nothing", ro(None, "1.0", 100.0, 200.0) == "")
    check("restart_owed: a superseded stage is not owed",
          ro({"version": "2.0", "current_version_at_download": "0.9"}, "1.0", 100.0, 200.0) == "")
    check("restart_owed: the version already running is not owed",
          ro({"version": "1.0"}, "1.0", 100.0, 200.0) == "")

    # --- loop guard ----------------------------------------------------------------
    check("may_restart: true at 0", ar.may_restart({}, "2.0") is True)
    check("may_restart: true at 1", ar.may_restart({"2.0": {"restarts": 1, "gave_up": False}}, "2.0") is True)
    check("may_restart: false at 2", ar.may_restart({"2.0": {"restarts": 2, "gave_up": False}}, "2.0") is False)
    check("may_restart: false once given up",
          ar.may_restart({"2.0": {"restarts": 0, "gave_up": True}}, "2.0") is False)
    check("may_restart: another version's count does not apply",
          ar.may_restart({"1.9": {"restarts": 2, "gave_up": True}}, "2.0") is True)
    s0 = {}
    s1 = ar.record_restart(s0, "2.0")
    s2 = ar.record_restart(s1, "2.0")
    check("record_restart counts per version",
          s1 == {"2.0": {"restarts": 1, "gave_up": False}}
          and s2 == {"2.0": {"restarts": 2, "gave_up": False}}, str(s2))
    check("record_restart does not change the dict it was given", s0 == {})

    idle = ar.idle_seconds()
    check("idle_seconds returns a float >= 0", isinstance(idle, float) and idle >= 0, str(idle))

    # --- main: development copy -----------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t), dev=True)
        env = Env()
        rc_ = ar.main(["--dry-run"], project_root=root, ops=env.ops())
        check("dev copy, --dry-run: returns 0", rc_ == 0, str(rc_))
        check("dev copy, --dry-run: taskkill never called", "taskkill" not in env.names(), str(env.names()))
        check("dev copy, --dry-run: nothing else run either (no channel check, apply, relaunch)",
              not {"channel_check", "auto_apply", "recover", "relaunch"} & set(env.names()))
        check("dev copy, --dry-run: logs 'development copy, not restarting'",
              "development copy, not restarting" in read(root / RLOG), read(root / RLOG))
        check("dev copy, --dry-run: no state file", state_of(root) is None)
        env = Env()
        ar.main([], project_root=root, ops=env.ops())
        check("dev copy, real run: refuses before anything is run", env.names() == [], str(env.names()))

    # --- main: nothing owed -----------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t), staged=False)
        env = Env()
        check("nothing owed: returns 0", ar.main([], project_root=root, ops=env.ops()) == 0)
        check("nothing owed: nothing stopped", "taskkill" not in env.names())
        check("nothing owed: no log line", read(root / RLOG) == "", read(root / RLOG))
        check("nothing owed: the hourly channel check still ran", "channel_check" in env.names())

    # --- main: applied by hand after Desktop started ------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t), staged=False)
        later = DESKTOP_START_EPOCH + 60
        os.utime(root / "VERSION", (later, later))
        env = Env(apply_result={"applied": False, "version": "", "reason": "no update", "restored": False})
        ar.main([], project_root=root, ops=env.ops())
        check("VERSION newer than Desktop's start: restarts to load it",
              ("taskkill", 34004) in env.calls and "relaunch" in env.names(), str(env.names()))
        check("...and counts it against the running version", state_of(root) == {"1.0": {"restarts": 1, "gave_up": False}},
              str(state_of(root)))

    # --- main: Desktop not running ---------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(procs=list(CLI_ROWS))
        ar.main([], project_root=root, ops=env.ops())
        check("no Desktop: nothing stopped (Claude Code left alone)", "taskkill" not in env.names())
        check("no Desktop: says so in the log", "not running" in read(root / RLOG), read(root / RLOG))
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(procs=None)
        env.list_processes = lambda: (env.calls.append(("list_processes",)), None)[1]
        ar.main([], project_root=root, ops=env.ops())
        check("process list unreadable: nothing stopped", "taskkill" not in env.names())

    # --- main: not idle ------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(idle=899.0)
        ar.main([], project_root=root, ops=env.ops())
        check("idle 899 s: nothing stopped", "taskkill" not in env.names())
        check("idle 899 s: logged as in use", "in use" in read(root / RLOG), read(root / RLOG))
        check("idle 899 s: no restart recorded", state_of(root) is None)
        env = Env(idle=899.0)
        ar.main(["--ignore-idle"], project_root=root, ops=env.ops())
        check("--ignore-idle: restarts anyway", ("taskkill", 34004) in env.calls)

    # --- main: the restart -----------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(idle=900.0)
        rc_ = ar.main([], project_root=root, ops=env.ops())
        names = env.names()
        kills = [c for c in env.calls if c[0] == "taskkill"]
        check("restart: returns 0", rc_ == 0)
        check("restart: stops exactly Desktop's root, once", kills == [("taskkill", 34004)], str(kills))
        check("restart: never a Claude Code pid", not any(c[1] in CLI_PIDS for c in kills))
        check("restart: Start menu entry read BEFORE stopping",
              "start_apps" in names and names.index("start_apps") < names.index("taskkill"), str(names))
        check("restart: waits for Desktop to be gone before installing",
              names.index("taskkill") < max(i for i, n in enumerate(names) if n == "list_processes")
              < names.index("auto_apply"), str(names))
        check("restart: recovers an unfinished apply first, then installs, not at startup",
              names.index("recover") < names.index("auto_apply")
              and ("auto_apply", root, False) in env.calls, str(names))
        check("restart: reopens through the Start menu entry, after installing",
              ("relaunch", STORE_CMD) in env.calls and names.index("relaunch") > names.index("auto_apply"))
        check("restart: recorded", state_of(root) == {"2.0": {"restarts": 1, "gave_up": False}},
              str(state_of(root)))
        check("restart: one log line naming the version",
              len(read(root / RLOG).splitlines()) == 1 and "2.0" in read(root / RLOG), read(root / RLOG))

    # --- main: cannot reopen -> never stop ----------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(start_apps=[])
        ar.main([], project_root=root, ops=env.ops())
        check("no way to reopen it: Desktop is not stopped", "taskkill" not in env.names(), str(env.names()))
        check("no way to reopen it: logged", "reopen" in read(root / RLOG), read(root / RLOG))
        check("no way to reopen it: no restart recorded", state_of(root) is None)

    # --- main: Desktop will not close ---------------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(after_kill=list(DESKTOP_ROWS + CLI_ROWS))
        ar.main([], project_root=root, ops=env.ops())
        check("Desktop still running after the wait: nothing installed",
              "auto_apply" not in env.names() and "recover" not in env.names(), str(env.names()))
        check("...waited about 30 s in total",
              29 <= sum(c[1] for c in env.calls if c[0] == "sleep") <= 31)
        check("...an [ERROR] line for the team", ERROR_LINE.search(read(root / VLOG)) is not None,
              read(root / VLOG))

    # --- main: giving up --------------------------------------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        (root / "data" / "auto_restart.json").write_text(
            json.dumps({"2.0": {"restarts": 2, "gave_up": False}}))
        envs = [Env(), Env()]
        for e in envs:
            ar.main([], project_root=root, ops=e.ops())
        vtext = read(root / VLOG)
        check("give up, run twice: nothing stopped either time",
              all("taskkill" not in e.names() for e in envs))
        check("give up, run twice: exactly one [ERROR] line",
              len(ERROR_LINE.findall(vtext)) == 1 and vtext.count("[ERROR]") == 1, vtext)
        check("give up: the [ERROR] line names the version", "2.0" in vtext)
        _st = state_of(root) or {}
        check("give up: recorded as given up, with when (gave_up_at, epoch seconds)",
              _st.get("2.0", {}).get("restarts") == 2 and _st["2.0"].get("gave_up") is True
              and _st["2.0"].get("gave_up_at") == DESKTOP_START_EPOCH + 3600, str(_st))

    # --- main: --dry-run on a real install changes nothing --------------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        before = {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()}
        env = Env()
        rc_ = ar.main(["--dry-run", "--ignore-idle"], project_root=root, ops=env.ops())
        after = {str(p.relative_to(root)): sha(p) for p in root.rglob("*") if p.is_file()
                 and p.relative_to(root) != RLOG}
        check("dry run, everything owed: returns 0", rc_ == 0)
        check("dry run: no channel check, stop, install or reopen",
              not {"channel_check", "taskkill", "recover", "auto_apply", "relaunch"} & set(env.names()),
              str(env.names()))
        check("dry run: no file changed except its own log line", after == before)
        check("dry run: says what it would do",
              "dry run" in read(root / RLOG) and "2.0" in read(root / RLOG), read(root / RLOG))

    # --- channel check is asked only through the stub, and not when due-gated off ------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(idle=0.0)
        boom = {"n": 0}

        def failing_check():
            boom["n"] += 1
            raise RuntimeError("channel unreachable")
        ops = env.ops()
        ops["channel_check"] = failing_check
        rc_ = ar.main([], project_root=root, ops=ops)
        check("a failing channel check does not stop the run", rc_ == 0 and boom["n"] == 1
              and "idle_seconds" in env.names(), str(env.names()))

    # --- T3-b: the main process is the one with no --type= -------------------------------------
    orphan = row(900, 777, STORE_EXE, "2026-11-18T07:00:00.0000000-07:00", "renderer")  # parent gone
    r = ar.desktop_root([orphan] + DESKTOP_ROWS + CLI_ROWS)
    check("T3-b: an orphaned --type=renderer helper older than the live main -> the live main",
          r is not None and r["ProcessId"] == 34004, str(r and r["ProcessId"]))
    two_mains = DESKTOP_ROWS + [row(950, 1, STORE_EXE, "2026-11-19T09:00:00.0000000-07:00")]
    check("T3-b: two Desktop processes without --type= -> no root", ar.desktop_root(two_mains) is None)
    check("T3-b: only helpers -> no root", ar.desktop_root(DESKTOP_ROWS[1:]) is None)
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(procs=two_mains + CLI_ROWS)
        ar.main([], project_root=root, ops=env.ops())
        check("T3-b: two mains -> refused: nothing stopped, nothing recorded",
              "taskkill" not in env.names() and state_of(root) is None, str(env.names()))
        check("T3-b: ...and the refusal is logged", "2.0" in read(root / RLOG) and read(root / RLOG) != "",
              read(root / RLOG))

    # --- T3-a: re-check the process right before stopping it ------------------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env()
        ar.main([], project_root=root, ops=env.ops())
        names = env.names()
        check("T3-a: processes are listed again immediately before taskkill",
              "taskkill" in names and names[names.index("taskkill") - 1] == "list_processes", str(names))
    reused = [dict(r_, CreationDate="2026-11-19T10:00:00.0000000-07:00") if r_["ProcessId"] == 34004 else r_
              for r_ in DESKTOP_ROWS] + CLI_ROWS
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(relist=reused)
        ar.main([], project_root=root, ops=env.ops())
        check("T3-a: same pid, different start time (pid reused) -> not stopped, not recorded",
              "taskkill" not in env.names() and "relaunch" not in env.names() and state_of(root) is None,
              str(env.names()))
    taken = [dict(r_, ExecutablePath=r"C:\Windows\notepad.exe") if r_["ProcessId"] == 34004 else r_
             for r_ in DESKTOP_ROWS] + CLI_ROWS
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(relist=taken)
        ar.main([], project_root=root, ops=env.ops())
        check("T3-a: the pid is no longer a Desktop executable -> not stopped",
              "taskkill" not in env.names(), str(env.names()))
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(relist=list(CLI_ROWS))
        ar.main([], project_root=root, ops=env.ops())
        check("T3-a: Desktop gone by the re-check -> not stopped", "taskkill" not in env.names())

    # --- C1(c): the exit wait is 30 s of wall time, however slow each listing is -----------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(after_kill=list(DESKTOP_ROWS + CLI_ROWS), list_cost=10.0)
        ar.main([], project_root=root, ops=env.ops())
        after_kill = env.names()[env.names().index("taskkill") + 1:] if "taskkill" in env.names() else []
        n_lists = after_kill.count("list_processes")
        check("C1: a 10 s listing ends the exit wait by wall time (at most 4 listings, not 31)",
              "taskkill" in env.names() and 1 <= n_lists <= 4, f"{n_lists} listings")
        check("C1: ...and nothing is installed", "auto_apply" not in env.names())

    # --- T3-d: Desktop is reopened whatever happens after it was closed ---------------------------
    for label, kw in (("recover raises", {"recover_raises": True}),
                      ("auto_apply raises", {"apply_raises": True})):
        with tempfile.TemporaryDirectory() as t:
            root = restart_tree(Path(t))
            env = Env(**kw)
            rc_ = ar.main([], project_root=root, ops=env.ops())
            check(f"T3-d: {label} -> Desktop still reopened", ("relaunch", STORE_CMD) in env.calls,
                  str(env.names()))
            check(f"T3-d: {label} -> an [ERROR] line, exit 1",
                  rc_ == 1 and ERROR_LINE.search(read(root / VLOG)) is not None, read(root / VLOG))
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        env = Env(relaunch_ok=False)
        ar.main([], project_root=root, ops=env.ops())
        vt = read(root / VLOG)
        check("T3-d: a failed reopen writes one [ERROR] saying so",
              len(ERROR_LINE.findall(vt)) == 1 and "reopen" in vt, vt)

    # --- I1: no restart owed for a version installed at startup ------------------------------------
    ro = ar.restart_owed
    _ro_params = inspect.signature(ro).parameters
    check("I1: restart_owed takes the startup-installed version", "startup_installed" in _ro_params)
    if "startup_installed" in _ro_params:
        check("I1: VERSION newer than Desktop, but that version was installed at startup -> nothing owed",
              ro(None, "2.0", 300.0, 200.0, startup_installed="2.0") == "")
        check("I1: ...a different version recorded -> still owed",
              ro(None, "2.0", 300.0, 200.0, startup_installed="1.9") == "2.0")
        check("I1: ...a staged update is owed whatever the record says",
              ro({"version": "3.0", "current_version_at_download": "2.0"}, "2.0", 300.0, 200.0,
                 startup_installed="2.0") == "3.0")
    # the reviewer's scenario: Desktop started 20 s ago, VERSION written now, nothing staged
    for with_record in (True, False):
        with tempfile.TemporaryDirectory() as t:
            root = restart_tree(Path(t), staged=False)
            later = DESKTOP_START_EPOCH + 20
            os.utime(root / "VERSION", (later, later))
            if with_record:
                (root / "data" / "startup_installed.json").write_text(json.dumps({"version": "1.0"}))
            env = Env(apply_result={"applied": False, "version": "", "reason": "", "restored": False})
            ar.main([], project_root=root, ops=env.ops())
            if with_record:
                check("I1: startup install of the running version recorded -> no restart, no log line",
                      "taskkill" not in env.names() and read(root / RLOG) == "", read(root / RLOG))
            else:
                check("I1: ...without the record -> the restart is owed",
                      ("taskkill", 34004) in env.calls, str(env.names()))

    # --- I5(b): a give-up resets once the person restarts Desktop by hand ---------------------------
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        (root / "data" / "auto_restart.json").write_text(json.dumps(
            {"2.0": {"restarts": 2, "gave_up": True, "gave_up_at": DESKTOP_START_EPOCH - 600}}))
        env = Env()
        ar.main([], project_root=root, ops=env.ops())
        check("I5b: Desktop started after the give-up -> tries again (restart 1)",
              ("taskkill", 34004) in env.calls
              and (state_of(root) or {}).get("2.0", {}).get("restarts") == 1
              and (state_of(root) or {}).get("2.0", {}).get("gave_up") is False, str(state_of(root)))
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        st = {"2.0": {"restarts": 2, "gave_up": True, "gave_up_at": DESKTOP_START_EPOCH + 600}}
        (root / "data" / "auto_restart.json").write_text(json.dumps(st))
        env = Env()
        ar.main([], project_root=root, ops=env.ops())
        check("I5b: Desktop started before the give-up -> still given up, nothing stopped",
              "taskkill" not in env.names() and state_of(root) == st, str(state_of(root)))
    with tempfile.TemporaryDirectory() as t:
        root = restart_tree(Path(t))
        st = {"2.0": {"restarts": 2, "gave_up": True, "gave_up_at": DESKTOP_START_EPOCH - 600}}
        (root / "data" / "auto_restart.json").write_text(json.dumps(st))
        env = Env()
        ar.main(["--dry-run"], project_root=root, ops=env.ops())
        check("I5b: dry run -> says it would try again, writes nothing",
              state_of(root) == st and "taskkill" not in env.names() and "dry run" in read(root / RLOG),
              read(root / RLOG))

    # --- hygiene -----------------------------------------------------------------------------------
    src = (SCRIPTS / "auto_restart.py").read_text(encoding="utf-8")
    n_calls = len(re.findall(r"subprocess\.(?:run|Popen)\(", src))
    check("every subprocess call closes stdin", n_calls > 0
          and src.count("stdin=subprocess.DEVNULL") >= n_calls, f"{n_calls} calls")
    check("no pipes, no capture_output", "PIPE" not in src and "capture_output" not in src)
    def _call_args(text, start):
        depth, i = 0, start
        while i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            if depth == 0 and text[i] == ")":
                return text[start:i]
            i += 1
        return text[start:]
    _runs = [_call_args(src, m.end() - 1) for m in re.finditer(r"subprocess\.run\(", src)]
    check("every subprocess.run has a timeout",
          bool(_runs) and all("timeout=" in a for a in _runs), f"{len(_runs)} calls")
    check("mcp_server is imported only lazily", not re.search(r"^(import|from) mcp_server", src, re.M))
    check("the checks wrote nothing to this machine's real state or log",
          (PROJECT_ROOT / "data" / "auto_restart.json").exists() == _real_state_before
          and read(PROJECT_ROOT / RLOG) == _real_rlog_before)


# =============================================================================
print("\n4. What the chat says")
import shutil  # noqa: E402
import mcp_server as ms  # noqa: E402

_real = {k: getattr(config, k) for k in ("PENDING_UPDATE_DIR", "INSTALLS_DIR", "DATA_DIR", "UPDATE_CHECK_STAMP_FILE")}
_real_fns = {k: getattr(ms, k, None) for k in ("_auto_update_active", "_write_install_checkin",
             "_ensure_deal_tracker_daily_run", "_ensure_restart_task", "_restart_task", "_RESTART_TASK_CHECKED")}
_tmp4 = Path(tempfile.mkdtemp())
try:
    # the pure wording function (the check_for_update tool itself is never called: its upkeep writes
    # the team's shared folder)
    um = getattr(ms, "_update_message", None)
    check("_update_message exists", um is not None)
    if um is not None:
        check("not active, update ready: the old 'ready to install' wording",
              "ready to install" in um("abc1234", "", False, "") and "apply_pending_update" in um("abc1234", "", False, ""))
        check("not active, restart owed: the old restart wording",
              "quit and reopen" in um("", "abc1234", False, "").lower())
        check("not active, nothing owed: says nothing", "Say nothing" in um("", "", False, ""))
        check("active, update ready: says nothing", "Say nothing" in um("abc1234", "", True, "")
              and "ready to install" not in um("abc1234", "", True, ""))
        check("active, restart owed: says nothing", "Say nothing" in um("", "abc1234", True, ""))
        check("active, gave up: asks for a full quit", "Quit" in um("abc1234", "", True, "abc1234"))

    # notice behaviour, with every side effect stubbed and the shared/real paths redirected
    for k, v in (("PENDING_UPDATE_DIR", _tmp4 / "pending"), ("INSTALLS_DIR", _tmp4 / "installs"),
                 ("DATA_DIR", _tmp4 / "data"), ("UPDATE_CHECK_STAMP_FILE", _tmp4 / "stamp.txt")):
        setattr(config, k, v)
        if k != "UPDATE_CHECK_STAMP_FILE":
            v.mkdir(parents=True, exist_ok=True)
    (_tmp4 / "stamp.txt").write_text("frozen")
    ms._write_install_checkin = lambda *a, **k: None
    ms._ensure_deal_tracker_daily_run = lambda *a, **k: ""
    ms._ensure_restart_task = lambda *a, **k: None
    (_tmp4 / "pending" / "ready.json").write_text(json.dumps({"version": "zzzzzz9", "zip_filename": "f.zip"}))
    body = "ANSWER" * 40

    ms._auto_update_active = lambda: True
    check("active + update staged: the notice adds nothing", ms._with_pending_notice("x", body) == body)
    (_tmp4 / "data" / "auto_restart.json").write_text(
        json.dumps({"zzzzzz9": {"restarts": 2, "gave_up": True}}))
    _gu = ms._with_pending_notice("x", body)
    check("active + gave up: the notice asks for a full quit", "Quit" in _gu and _gu.startswith(body))
    check("_auto_restart_gave_up names the version", getattr(ms, "_auto_restart_gave_up", lambda: "")() == "zzzzzz9")
    (_tmp4 / "data" / "auto_restart.json").unlink()
    check("no state file: not gave up", getattr(ms, "_auto_restart_gave_up", lambda: "x")() == "")

    ms._auto_update_active = lambda: False
    check("not active: the old 'ready to install' wording returns",
          "ready to install" in ms._with_pending_notice("x", body))

    # the real predicate
    check("_auto_update_active is False on a development copy (this tree is one)",
          _real_fns["_auto_update_active"]() is False)

    # registration, with the scheduler call stubbed
    ms._ensure_restart_task = _real_fns["_ensure_restart_task"]
    calls = []
    ms._restart_task = lambda action: (calls.append(action), "not found" if action == "exists" else "")[1]
    ms._RESTART_TASK_CHECKED = False
    ms._auto_update_active = lambda: False
    ms._ensure_restart_task()
    check("not active: _restart_task is never called", calls == [], str(calls))
    ms._auto_update_active = lambda: True
    ms._ensure_restart_task()
    ms._ensure_restart_task()
    check("active, not found: 'add' called exactly once over two calls",
          calls == ["exists", "add"], str(calls))
    calls.clear()
    ms._RESTART_TASK_CHECKED = False
    ms._restart_task = lambda action: (calls.append(action), "")[1]
    ms._ensure_restart_task()
    check("active, already registered: no 'add'", calls == ["exists"], str(calls))
    ms._RESTART_TASK_CHECKED = False

    def _boom(action):
        raise RuntimeError("schtasks missing")
    ms._restart_task = _boom
    try:
        ms._ensure_restart_task()
        check("a failing scheduler never raises", True)
    except Exception as e:
        check("a failing scheduler never raises", False, str(e))

    src_ms = (PROJECT_ROOT / "mcp_server.py").read_text(encoding="utf-8")
    try:
        seg = src_ms[src_ms.index("def _restart_task"):src_ms.index("def _ensure_restart_task")]
    except ValueError:
        seg = ""
    check("the task: name, interactive principal, 15 minutes, env vars, stdin closed",
          "Vaulter AI - Update restart" in src_ms and "-LogonType Interactive" in seg
          and "-Minutes 15" in seg and "VLT_RUNNER" in seg and "stdin=subprocess.DEVNULL" in seg
          and "auto_restart_schedule.txt" in seg and "-RunLevel" not in seg and "-Password" not in seg)
finally:
    for k, v in _real.items():
        setattr(config, k, v)
    for k, v in _real_fns.items():
        if v is not None:
            setattr(ms, k, v)
    shutil.rmtree(_tmp4, ignore_errors=True)


failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
