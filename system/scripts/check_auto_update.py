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


def row(pid, ppid, exe, created=DESKTOP_START):
    return {"ProcessId": pid, "ParentProcessId": ppid, "ExecutablePath": exe,
            "CommandLine": f'"{exe}"', "CreationDate": created}


# Recorded on this machine 2026-11-19: Desktop parent 34004 (its parent 12308 is
# not Desktop) with eight children; four Claude Code CLI processes.
DESKTOP_ROWS = [row(34004, 12308, STORE_EXE)] + [
    row(p, 34004, STORE_EXE, "2026-11-19T08:00:05.0000000-07:00")
    for p in (24540, 9492, 9420, 4912, 21040, 35000, 22996, 22764)]
CLI_ROWS = [row(15912, 8356, CLI_EXE), row(18916, 7284, CLI_EXE),
            row(33016, 18292, CLI_EXE), row(18024, 33524, CLI_EXE)]
CLI_PIDS = {r["ProcessId"] for r in CLI_ROWS}
START_APPS = [{"Name": "Claude", "AppID": "Claude_pzs8sxrjxfjjc!Claude"}]
STORE_CMD = ["explorer.exe", "shell:AppsFolder\\Claude_pzs8sxrjxfjjc!Claude"]


class Env:
    """A stubbed world for auto_restart.main. `calls` records every effect in order."""

    def __init__(self, procs=None, after_kill=None, idle=1000.0, start_apps=None,
                 apply_result=None):
        self.procs = list(DESKTOP_ROWS + CLI_ROWS) if procs is None else procs
        self.after_kill = list(CLI_ROWS) if after_kill is None else after_kill
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
        }

    def names(self):
        return [c[0] for c in self.calls]

    def list_processes(self):
        self.calls.append(("list_processes",))
        return list(self.after_kill if self.killed else self.procs)

    def start_apps(self):
        self.calls.append(("start_apps",))
        return self.apps

    def taskkill(self, pid):
        self.calls.append(("taskkill", pid))
        self.killed = True
        return 0

    def relaunch(self, cmd):
        self.calls.append(("relaunch", list(cmd)))
        return True

    def idle_seconds(self):
        self.calls.append(("idle_seconds",))
        return self.idle

    def channel_check(self):
        self.calls.append(("channel_check",))

    def recover(self, project_root):
        self.calls.append(("recover", project_root))
        return False

    def auto_apply(self, project_root, at_startup=False):
        self.calls.append(("auto_apply", project_root, at_startup))
        return dict(self.apply_result)

    def sleep(self, seconds):
        self.calls.append(("sleep", seconds))


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
    classic = [row(70, 9, CLASSIC_EXE), row(71, 70, CLASSIC_EXE)] + CLI_ROWS
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
        check("give up: recorded as given up",
              state_of(root) == {"2.0": {"restarts": 2, "gave_up": True}}, str(state_of(root)))

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


failed = [r for r in RESULTS if not r[1]]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
