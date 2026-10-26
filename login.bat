@echo off
setlocal
title Edge Trader for Schwab: sign in
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
rem Signs in to Schwab through schwabdev (https://github.com/tylerebowers/Schwabdev). Run again about once a week.
".venv\Scripts\python.exe" -m companion.login %*
pause
