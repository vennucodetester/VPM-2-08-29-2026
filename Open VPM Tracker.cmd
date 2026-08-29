@echo off
setlocal
title VPM Tracker
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0tracker_app2.py"
    exit /b
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%~dp0tracker_app2.py"
    exit /b
)

echo Python was not found. Install Python, then try this launcher again.
pause
