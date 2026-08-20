@echo off
REM ---------------------------------------------------------------------------
REM Vaulter AI -- the daily morning round.
REM
REM Runs from Windows Task Scheduler on ONE designated machine. This project
REM deliberately runs nothing in the background inside the program itself; a
REM scheduled task on one machine is the sanctioned way to have something
REM happen on a clock, and the nightly file-list refresh already works this way.
REM
REM Two layers on purpose:
REM   1. team_status.py gathers the countable facts. Free, no model, and it
REM      cannot get a number wrong.
REM   2. Claude reads those facts and writes the briefing -- the judgement about
REM      what actually matters today, which is the part a script cannot do.
REM
REM If layer 2 fails, layer 1's output is still saved, so the morning is never a
REM total blank.
REM
REM ANTHROPIC_API_KEY is cleared for this run on purpose. When it is set it takes
REM precedence over the signed-in Claude account, and on this machine that key
REM has no credit -- so the round would fail with "credit balance too low" while
REM looking like a scheduling problem. Verified 2026-08-20.
REM ---------------------------------------------------------------------------

setlocal

set "PROJECT=%~dp0..\.."
cd /d "%PROJECT%"

set "ANTHROPIC_API_KEY="
set "ANTHROPIC_AUTH_TOKEN="

set "OUTDIR=%PROJECT%\system\data\logs"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"
set "RAW=%OUTDIR%\daily_round_facts.txt"
set "RUNLOG=%OUTDIR%\daily_round.log"

echo === %DATE% %TIME% : starting the morning round >>"%RUNLOG%"

REM Layer 1: the facts. Saved whatever happens next.
python "%PROJECT%\system\scripts\team_status.py" >"%RAW%" 2>&1
if errorlevel 1 echo   the fact-gathering step reported a problem >>"%RUNLOG%"

echo   facts gathered >>"%RUNLOG%"

REM Layer 2: the briefing. Read-only tools only -- it looks and reports, and is
REM told in its own prompt not to change, publish or apply anything.
REM
REM The prompt is REDIRECTED from the file, not piped in with `type`. Piping
REM builds a two-process pipeline, and under Task Scheduler the run then died
REM with 0xC000013A -- the code for a console being torn down -- after the facts
REM step had already finished. A redirect hands the same bytes to one process
REM with no second console involved. Measured 2026-08-20; it does not reproduce
REM when the same file is run by hand, which is the whole reason this is worth a
REM comment.
claude -p --allowed-tools "Bash(python:*)" "Read" "Write" "Glob" "Grep" ^
  <"%PROJECT%\system\scripts\daily_round_prompt.txt" >>"%RUNLOG%" 2>&1

if errorlevel 1 (
    echo   the briefing step failed -- the facts are still in %RAW% >>"%RUNLOG%"
) else (
    echo   done >>"%RUNLOG%"
)

endlocal
