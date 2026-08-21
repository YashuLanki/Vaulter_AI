@echo off
REM ---------------------------------------------------------------------
REM Double-click this if Vaulter AI does not seem to be working.
REM
REM It starts the program the same way Claude Desktop does, tells you in
REM plain English whether it worked, and sends the answer to the team
REM folder so nobody has to ask you for a screenshot.
REM
REM It only looks. It changes nothing, downloads nothing, and installs
REM nothing.
REM ---------------------------------------------------------------------

setlocal
cd /d "%~dp0.."

set PYCMD=
for %%C in (python py python3) do call :try_python %%C

if not defined PYCMD (
    echo.
    echo Python was not found on this computer, so this check cannot run.
    echo Run "Setup Vaulter AI" in this same folder first.
    echo.
    pause
    exit /b 1
)

%PYCMD% "%CD%\system\scripts\check_my_connection.py"

echo.
echo Press any key to close this window.
pause >nul
endlocal
exit /b 0

:try_python
if defined PYCMD goto :eof
%1 --version >nul 2>&1 || goto :eof
REM Must be 3.10 or newer, the same bar the installer sets.
%1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1 || goto :eof
set PYCMD=%1
goto :eof