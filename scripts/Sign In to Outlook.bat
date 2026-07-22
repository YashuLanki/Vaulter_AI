@echo off
REM Double-click this file to sign into your own Microsoft/Outlook account.
REM A short code and a web address will appear below -- open that address
REM in your browser and enter the code to finish signing in.
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
echo Python was not found on this computer.
echo Install it from https://www.python.org/downloads/ first -- during
echo setup, make sure to tick "Add python.exe to PATH" -- then double-click
echo this file again.
echo.
pause
exit /b 1

:run
%PYCMD% main.py auth
echo.
pause
