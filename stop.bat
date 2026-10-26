@echo off
rem Stops the Edge Trader server (python -m companion). Your orders at Schwab are not affected.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -match '-m companion($| )' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped Edge Trader pid ' + $_.ProcessId) }"
ping -n 3 127.0.0.1 >nul
