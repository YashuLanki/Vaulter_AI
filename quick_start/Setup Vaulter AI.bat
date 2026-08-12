@echo off
REM Double-click this file to run the Vaulter AI setup wizard.
REM (No terminal or typed commands needed -- this window just shows its progress.)
cd /d "%~dp0"

REM ---- Move somewhere safe before anything else runs --------------------
REM Three locations break an install, in increasing order of nastiness, and
REM a non-technical user lands in all three by doing the obvious thing:
REM
REM   OneDrive   -- can lock or partially-sync the files this system writes
REM                 to constantly (the search index, screening output).
REM   Downloads  -- setup records this exact path in Claude Desktop's config,
REM                 and Downloads is the folder people tidy up.
REM   Inside the zip -- the worst. Double-clicking this file straight out of
REM                 the zip makes Windows extract to %TEMP%\Temp1_<name>.zip\
REM                 and run it there. Setup would appear to succeed, spend
REM                 minutes indexing, wire up Claude Desktop -- and then
REM                 Windows deletes the folder. The connector dies later with
REM                 no visible cause. Measured 2026-08-12; nothing warned.
REM
REM All three are fixed the same way: copy to a permanent local folder and
REM carry on from there, rather than asking someone to fix a path by hand.
for %%I in ("%~dp0..") do set "VAULTER_ROOT=%%~fI"

REM NEVER end a findstr /C:"..." pattern with a backslash. Windows reads the
REM closing \" as an escaped quote, the argument breaks, and the check silently
REM never matches -- so the folder looks safe and setup carries on into a temp
REM directory. Measured 2026-08-12: "\Downloads\" never matched while
REM "\Downloads" matched correctly. Leading backslash only, always.
set "MOVE_WHY="
echo %VAULTER_ROOT%| findstr /I /L /C:"OneDrive" >nul
if not errorlevel 1 set "MOVE_WHY=onedrive"
echo %VAULTER_ROOT%| findstr /I /L /C:"\Downloads" >nul
if not errorlevel 1 set "MOVE_WHY=downloads"
echo %VAULTER_ROOT%| findstr /I /L /C:"\AppData\Local\Temp" >nul
if not errorlevel 1 set "MOVE_WHY=zip"
echo %VAULTER_ROOT%| findstr /I /L /C:"Temp1_" >nul
if not errorlevel 1 set "MOVE_WHY=zip"
echo %VAULTER_ROOT%| findstr /I /L /C:"\Temporary Internet Files" >nul
if not errorlevel 1 set "MOVE_WHY=zip"

if defined MOVE_WHY goto :relocate
goto :python_check

:relocate
set "TARGET=%USERPROFILE%\Vaulter AI"

REM If the program files aren't beside this launcher, we were run from a
REM partial extraction -- copying now would install a broken folder. Say so
REM instead, in the one sentence that actually fixes it.
if not exist "%VAULTER_ROOT%\system\scripts\setup_wizard.py" (
    if not exist "%VAULTER_ROOT%\scripts\setup_wizard.py" (
        echo.
        echo ============================================================
        echo   Please unzip first
        echo ============================================================
        echo.
        echo   It looks like this was opened from inside the zip file, so
        echo   only part of it is available and setup can't run yet.
        echo.
        echo   To fix it:
        echo     1. Close this window.
        echo     2. Right-click the "Vaulter AI" zip file and choose
        echo        "Extract All...", then click Extract.
        echo     3. Open the folder that appears, go into quick_start,
        echo        and double-click "Setup Vaulter AI" again.
        echo.
        pause
        exit /b 1
    )
)

echo.
echo ============================================================
echo   Moving Vaulter AI to a permanent folder
echo ============================================================
echo.
if "%MOVE_WHY%"=="onedrive" (
    echo   This folder is inside OneDrive, which can interfere with files
    echo   Vaulter AI writes to constantly -- its search index and
    echo   screening results.
)
if "%MOVE_WHY%"=="downloads" (
    echo   This folder is in your Downloads, which is the folder most
    echo   people clear out. Setup records this exact location, so
    echo   deleting it later would quietly break Vaulter AI.
)
if "%MOVE_WHY%"=="zip" (
    echo   This was started from inside the zip file, so it's running in a
    echo   temporary folder that Windows deletes on its own. Setup would
    echo   look like it worked and then stop working later.
)
echo.
echo   Copying it now to a permanent folder instead:
echo.
echo     %TARGET%
echo.
if exist "%TARGET%" (
    echo   Vaulter AI is already installed there.
    echo.
    echo   Nothing has been changed. To finish setting that copy up, open:
    echo     %TARGET%\quick_start
    echo   and double-click "Setup Vaulter AI" there. It is safe to run more
    echo   than once.
    echo.
    echo   If you meant to replace it with this newer copy, rename or delete
    echo   the folder above first, then double-click Setup here again.
    echo.
    pause
    exit /b 1
)
echo   This takes a few seconds. Please wait...
robocopy "%VAULTER_ROOT%" "%TARGET%" /E /NFL /NDL /NJH /NJS /NP >nul
if %errorlevel% GEQ 8 (
    echo.
    echo   The copy didn't complete. Please copy this "Vaulter AI" folder to
    echo   %TARGET% yourself, then double-click Setup from there.
    echo.
    pause
    exit /b 1
)
echo   Done. Continuing setup from the new location -- no need to click
echo   anything else. (Once this finishes, the copy you started from can
echo   be deleted; this new folder is the one that matters.)
echo.
cd /d "%TARGET%\quick_start"
goto :python_check

:python_check
where python >nul 2>nul
if %errorlevel%==0 (
    set PYCMD=python
    goto :run
)

where py >nul 2>nul
if %errorlevel%==0 (
    set PYCMD=py
    goto :run
)

echo.
echo Python was not found on this computer. Vaulter AI needs it to run.
echo.
echo This can install it for you now: it downloads the official installer
echo directly from python.org (about 27 MB), and installs it just for your
echo own Windows account -- no admin rights needed, and nothing else on
echo this computer is touched.
echo.
echo Press any key to do that now, or close this window if you'd rather
echo install Python yourself from https://www.python.org/downloads/ first.
pause >nul

set PYINSTALLER=%TEMP%\vaulter_python_installer.exe
echo.
echo Downloading Python from python.org...
curl -fL -o "%PYINSTALLER%" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
if not %errorlevel%==0 (
    echo.
    echo The download didn't go through -- possibly no internet connection,
    echo or a firewall blocked it. Please install Python yourself instead:
    echo https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
    echo then double-click this file again.
    echo.
    pause
    exit /b 1
)

echo Installing Python for your account only -- a progress window will
echo show briefly...
"%PYINSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=0 Include_test=0
del "%PYINSTALLER%" >nul 2>nul

set PYCMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if exist "%PYCMD%" (
    echo.
    echo Python installed. Continuing with Vaulter AI setup...
    goto :run
)

echo.
echo Python installed, but this window couldn't confirm it right away.
echo Please close this window and double-click "Setup Vaulter AI" again --
echo it should find Python this time.
echo.
pause
exit /b 1

:run
REM Works in both layouts without needing two versions of this file: the
REM development checkout (scripts\ sits beside quick_start\) and the packaged
REM handoff folder built by scripts\build_handoff.py (everything tucked into
REM system\). Checked in that order; if neither exists the folder is incomplete.
set WIZARD=..\scripts\setup_wizard.py
if not exist "%WIZARD%" set WIZARD=..\system\scripts\setup_wizard.py
if not exist "%WIZARD%" (
    echo.
    echo This folder looks incomplete -- the setup files couldn't be found.
    echo Please ask whoever sent it to you for a fresh copy.
    echo.
    pause
    exit /b 1
)

"%PYCMD%" "%WIZARD%"
echo.
pause
