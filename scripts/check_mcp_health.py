"""
check_mcp_health.py
--------------------
Vaulter AI -- deterministic MCP connector health check.

Spawns `python main.py mcp` exactly the way Claude Desktop/Code does (a real
stdio subprocess, not an in-process import) and drives it through the same
handshake a real client would: initialize -> list_tools -> call a couple of
tools, with real wall-clock timing on each step. That distinction matters --
calling a tool function directly in-process can look fast while the same
call over the real stdio transport hangs (this is exactly how the
2026-07-30 check_system_health hang was found: it never reproduced through a
direct call, only through a genuine subprocess).

Usage:
  python scripts/check_mcp_health.py

Exits 0 if everything checked out, 1 if anything looks wrong. Prints a plain
PASS/FAIL report either way -- this is meant to be run by a human or a
subagent (vaulter-connection-doctor), not parsed by other code.

This does NOT check corpus/shared-folder/portfolio connectivity in detail --
check_system_health already reports that, and this script's own call to it
surfaces whatever it says. This script's job is the layer underneath that:
is the server itself reachable, fast, and correctly wired, regardless of
what it finds once it gets there.
"""

import asyncio
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
LOG_FILE = PROJECT_ROOT / "data" / "logs" / "vaulter.log"

# Not maintained as an exact name list on purpose (see CLAUDE.md's own note
# that a hand-kept tool list drifted before) -- a count sanity check catches
# an accidentally-dropped or accidentally-duplicated tool without needing to
# be updated every time a tool is intentionally added or renamed.
EXPECTED_TOOL_COUNT = 21

# Generous on purpose: the 2026-07-30 fix bounds check_system_health's own
# slow path (a stuck git subprocess) to ~5s. Anything past 15s means either
# that fix regressed or a new slow path was introduced.
SLOW_CALL_THRESHOLD_SECONDS = 15


async def _run_checks() -> list[str]:
    """Returns a list of problem descriptions; empty means everything passed."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    problems = []
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable

    params = StdioServerParameters(
        command=python_exe,
        args=["main.py", "mcp"],
        cwd=str(PROJECT_ROOT),
    )

    t_start = time.perf_counter()
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                t0 = time.perf_counter()
                await session.initialize()
                init_took = time.perf_counter() - t0
                print(f"  initialize: {init_took:.1f}s")
                if init_took > SLOW_CALL_THRESHOLD_SECONDS:
                    problems.append(f"initialize took {init_took:.1f}s (expected a few seconds)")

                t0 = time.perf_counter()
                tools = await session.list_tools()
                list_took = time.perf_counter() - t0
                count = len(tools.tools)
                print(f"  list_tools: {list_took:.1f}s, {count} tools")
                if count != EXPECTED_TOOL_COUNT:
                    problems.append(
                        f"tool count is {count}, expected {EXPECTED_TOOL_COUNT} -- "
                        f"a tool may have failed to register, or this constant needs "
                        f"updating after an intentional change"
                    )

                for tool_name, args in (("check_system_health", {}), ("get_portfolio_list", {})):
                    t0 = time.perf_counter()
                    try:
                        result = await asyncio.wait_for(
                            session.call_tool(tool_name, args),
                            timeout=SLOW_CALL_THRESHOLD_SECONDS + 10,
                        )
                        took = time.perf_counter() - t0
                        print(f"  call_tool({tool_name}): {took:.1f}s")
                        if took > SLOW_CALL_THRESHOLD_SECONDS:
                            problems.append(f"{tool_name} took {took:.1f}s (over the {SLOW_CALL_THRESHOLD_SECONDS}s bar)")
                        if result.isError:
                            problems.append(f"{tool_name} returned an error: {result}")
                    except (TimeoutError, asyncio.TimeoutError):
                        problems.append(f"{tool_name} did not respond within {SLOW_CALL_THRESHOLD_SECONDS + 10}s")
                    except Exception as e:
                        problems.append(f"{tool_name} raised {type(e).__name__}: {e}")
    except Exception as e:
        problems.append(f"could not complete the stdio handshake at all: {type(e).__name__}: {e}")

    total = time.perf_counter() - t_start
    print(f"  total: {total:.1f}s")
    return problems


def _check_recent_log(minutes: int = 60) -> list[str]:
    """Recent ERROR/CRITICAL/timeout lines, not the whole file's history."""
    if not LOG_FILE.exists():
        return []
    cutoff = datetime.now() - timedelta(minutes=minutes)
    problems = []
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-2000:]  # bounded read on a file that can exceed tool size limits
    except OSError as e:
        return [f"could not read {LOG_FILE.name}: {e}"]

    hits = []
    for line in lines:
        try:
            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if ts < cutoff:
            continue
        if "[ERROR]" in line or "[CRITICAL]" in line or "Traceback" in line or "Request timed out" in line:
            hits.append(line.rstrip())

    if hits:
        problems.append(f"{len(hits)} error/timeout line(s) in the log in the last {minutes} minutes:")
        problems.extend(f"    {h}" for h in hits[:10])
    return problems


def main() -> int:
    print("Vaulter AI -- MCP connector health check")
    print(f"Project root: {PROJECT_ROOT}\n")

    print("Driving a real stdio session against a fresh `python main.py mcp`:")
    problems = asyncio.run(_run_checks())

    print("\nScanning the recent log for errors:")
    log_problems = _check_recent_log()
    if log_problems:
        for p in log_problems:
            print(f"  {p}")
    else:
        print("  clean")
    problems.extend(log_problems)

    print()
    if problems:
        print(f"FAIL -- {len(problems)} problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        return 1

    print("PASS -- MCP connector is healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
