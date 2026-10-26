@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
set PORT=8770
if defined COMPANION_PORT set PORT=%COMPANION_PORT%
rem Already running? Just open the dashboard.
netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if %errorlevel%==0 (
    start "" http://127.0.0.1:%PORT%
    exit /b
)
rem Windowless server: no console window to close by accident. Output and errors go to data\companion.log.
start "" ".venv\Scripts\pythonw.exe" -m companion
timeout /t 5 /nobreak >nul
start "" http://127.0.0.1:%PORT%
