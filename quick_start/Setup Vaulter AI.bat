@echo off
REM Double-click this file to run the Vaulter AI setup wizard.
REM (No terminal or typed commands needed -- this window just shows its progress.)
cd /d "%~dp0"

REM ---- Move out of OneDrive before anything else runs -------------------
REM OneDrive can lock or partially-sync files this system writes to
REM constantly (the search index, screening output), and asking a
REM non-technical user to relocate the folder by hand -- editing the path,
REM deleting folder name segments -- is exactly the friction this removes.
for %%I in ("%~dp0..") do set "VAULTER_ROOT=%%~fI"
echo %VAULTER_ROOT%| findstr /I "OneDrive" >nul
if not errorlevel 1 goto :relocate
goto :python_check

:relocate
set "TARGET=%USERPROFILE%\Vaulter AI"
echo.
echo ============================================================
echo   Moving Vaulter AI to a local folder
echo ============================================================
echo.
echo   This folder is inside OneDrive, which can interfere with files
echo   Vaulter AI writes to constantly -- its search index and
echo   screening results. Copying it now to a local folder that
echo   OneDrive won't touch:
echo.
echo     %TARGET%
echo.
if exist "%TARGET%" (
    echo   A folder already exists there, so this can't be done automatically.
    echo   Please move or rename that folder, then double-click Setup again.
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
echo   anything else. (The old copy in OneDrive can be deleted once this
echo   finishes.)
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
