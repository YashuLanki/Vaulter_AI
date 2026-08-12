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

call :check_location

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

REM Never copy one of the user's own major folders. If someone unpacked with
REM "Extract Here" in another tool, or moved files by hand, quick_start can end
REM up sitting directly in Downloads or Documents -- and then VAULTER_ROOT is
REM that whole folder, so the copy below would drag every unrelated file the
REM user owns into the install (measured: 3.4 GB and 62 items in one real
REM Downloads folder). Checked BEFORE the "moving it" banner, so the window
REM never announces a move it is not going to make.
for %%K in ("%USERPROFILE%" "%USERPROFILE%\Downloads" "%USERPROFILE%\Documents" "%USERPROFILE%\Desktop") do (
    if /I "%VAULTER_ROOT%"=="%%~fK" set "IS_USER_FOLDER=1"
)
if defined IS_USER_FOLDER (
    echo.
    echo ============================================================
    echo   Please unzip into its own folder
    echo ============================================================
    echo.
    echo   The Vaulter AI files look like they were unpacked loose into
    echo     %VAULTER_ROOT%
    echo   so they're mixed in with your other files there. Setup has
    echo   stopped rather than risk moving them.
    echo.
    echo   To fix it:
    echo     1. Close this window.
    echo     2. Find the "Vaulter AI" zip you downloaded, right-click it and
    echo        choose "Extract All...", then click Extract.
    echo     3. Open the folder that appears, go into quick_start, and
    echo        double-click "Setup Vaulter AI".
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Moving Vaulter AI to a permanent folder
echo ============================================================
echo.
if "%MOVE_WHY%"=="cloud" (
    echo   This folder is inside a cloud-synced folder, which can lock or
    echo   part-sync the files Vaulter AI writes to constantly -- its search
    echo   index and screening results.
)
if "%MOVE_WHY%"=="onedrive" (
    echo   This folder is inside OneDrive, which can interfere with files
    echo   Vaulter AI writes to constantly -- its search index and
    echo   screening results.
)
if "%MOVE_WHY%"=="removable" (
    echo   This folder is on a removable drive, such as a USB stick. Setup
    echo   records this exact location, so Vaulter AI would stop working
    echo   every time the drive isn't plugged in.
)
if "%MOVE_WHY%"=="network" (
    echo   This folder is on a network drive. Setup records this exact
    echo   location, so Vaulter AI would stop working whenever you're off
    echo   the network, and it would be slow even when you're on it.
)
if "%MOVE_WHY%"=="readonly" (
    echo   This folder can't be written to, so Vaulter AI has nowhere to
    echo   keep its search index or your screening results.
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
set "PYEXIT=%errorlevel%"
del "%PYINSTALLER%" >nul 2>nul

REM Look in several places, not one exact path. The old version checked only
REM %LOCALAPPDATA%\Programs\Python\Python312 and, on anything else, told the
REM user to double-click Setup again -- which is the confusing second click,
REM and it never worked when the install had actually failed.
set "PYCMD="
for %%V in (Python313 Python312 Python311) do (
    if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\%%V\python.exe" set "PYCMD=%LOCALAPPDATA%\Programs\Python\%%V\python.exe"
)
if not defined PYCMD for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do (
    if not defined PYCMD set "PYCMD=%%P"
)
if not defined PYCMD if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PYCMD=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"

if defined PYCMD (
    echo.
    echo Python installed. Continuing with Vaulter AI setup...
    goto :run
)

REM Nothing found. Say so plainly and STOP. Measured 2026-08-12 on a real
REM bare-machine run: the installer returned a failure code, installed
REM nothing at all, and the old wording still said "Python installed" and
REM sent the user round to double-click Setup again -- which fails the same
REM way every time, with no way out and no clue why. Claiming success we did
REM not verify is worse than admitting failure.
echo.
echo ============================================================
echo   Python could not be installed automatically
echo ============================================================
echo.
echo   The installer finished with code %PYEXIT% and Python is not on this
echo   computer, so setup cannot continue. This is a Windows problem
echo   rather than anything wrong with Vaulter AI, and it is usually
echo   quicker to install Python yourself:
echo.
echo     1. Go to   https://www.python.org/downloads/
echo     2. Download Python 3.12, run it, and TICK the box that says
echo        "Add python.exe to PATH" on the first screen.
echo     3. Come back here and double-click "Setup Vaulter AI" again.
echo.
echo   If that also fails, send this whole window to Yashu -- the code
echo   above says why.
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
exit /b 0


REM ======================================================================
REM  Is this folder a safe place to install into?
REM ======================================================================
REM Asks what the location IS, not what it is CALLED. An earlier version
REM listed known-bad folder names, which can only ever cover the places
REM someone thought of -- a redirected temp folder, a USB stick, a mapped
REM network drive, a cloud folder with a custom name all slipped through.
REM Every question below is answered from the machine at run time.
REM
REM Two traps, both measured 2026-08-12, both silent when got wrong:
REM
REM  1. A findstr /C:"..." pattern must NEVER end with a backslash: Windows
REM     reads the closing \" as an escaped quote, the argument breaks, and
REM     the test never matches. "\Downloads\" never fired; "\Downloads" did.
REM  2. Windows hands out %TEMP% in short 8.3 form (C:\Users\ABCDEF~1\...)
REM     while Explorer gives the long form, so comparing them directly MISSES.
REM     Both sides are converted to short form (%%~sI) before comparing --
REM     that direction always works, the reverse does not.
REM ======================================================================
:check_location
set "MOVE_WHY="
for %%I in ("%VAULTER_ROOT%") do set "VR_SHORT=%%~sI"
for %%I in ("%VAULTER_ROOT%") do set "VR_DRIVE=%%~dI"

REM -- Can we even write here? (read-only share, locked-down disc, CD) ----
set "WRITE_PROBE=%VAULTER_ROOT%\vaulter_write_probe.tmp"
break > "%WRITE_PROBE%" 2>nul
if not exist "%WRITE_PROBE%" set "MOVE_WHY=readonly"
del "%WRITE_PROBE%" >nul 2>nul

REM -- What KIND of drive is this? Removable and network drives disappear -
for /f "tokens=*" %%T in ('fsutil fsinfo drivetype %VR_DRIVE% 2^>nul') do set "DTYPE=%%T"
echo %DTYPE%| findstr /I /L /C:"Removable" >nul
if not errorlevel 1 set "MOVE_WHY=removable"
echo %DTYPE%| findstr /I /L /C:"Network" >nul
if not errorlevel 1 set "MOVE_WHY=network"
echo %DTYPE%| findstr /I /L /C:"CD-ROM" >nul
if not errorlevel 1 set "MOVE_WHY=readonly"

REM -- Inside a temporary folder? Read the real ones, don't assume a name -
call :is_inside "%TEMP%" zip
call :is_inside "%TMP%" zip

REM -- Inside Downloads? Ask Windows where that actually is; it can be
REM    renamed, redirected to another drive, or localised.
for /f "tokens=2,*" %%A in ('reg query "HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders" /v "{374DE290-123F-4565-9164-39C4925E467B}" 2^>nul') do call :is_inside "%%B" downloads
if not defined MOVE_WHY call :is_inside "%USERPROFILE%\Downloads" downloads

REM -- Inside any cloud-synced folder? OneDrive publishes its real roots as
REM    environment variables whatever the tenant folder is named, so this
REM    needs no company name and works on any machine.
for /f "tokens=1,* delims==" %%A in ('set OneDrive 2^>nul') do call :is_inside "%%B" onedrive

REM -- Other sync clients publish no such variable, so these stay by name.
REM    A residual gap, and the reason the wizard ALSO warns about where it
REM    is running from: this is one of two nets, not the only one.
for %%C in (Dropbox "Google Drive" GoogleDrive Box iCloudDrive Nextcloud ownCloud pCloud MEGAsync Syncthing) do call :is_inside "%USERPROFILE%\%%~C" cloud
goto :eof


REM -- Is VAULTER_ROOT inside the folder named in %1? Sets MOVE_WHY=%2 ----
REM    Skips an empty, missing, or bare-drive-root candidate: findstr with an
REM    empty pattern errors, and "C:\" would match every path on the disk.
:is_inside
if "%~1"=="" goto :eof
if not exist "%~1" goto :eof
for %%I in ("%~1") do set "CAND=%%~sI"
if "%CAND%"=="" goto :eof
if "%CAND:~-2%"==":\" goto :eof
echo %VR_SHORT%| findstr /I /L /C:"%CAND%" >nul
if not errorlevel 1 set "MOVE_WHY=%~2"
goto :eof
