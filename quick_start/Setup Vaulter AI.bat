@echo off
REM Double-click this file to run the Vaulter AI setup wizard.
REM (No terminal or typed commands needed -- this window just shows its progress.)
cd /d "%~dp0"

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
"%PYCMD%" "..\scripts\setup_wizard.py"
echo.
pause
