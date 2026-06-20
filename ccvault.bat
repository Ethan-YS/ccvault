@echo off
REM Double-click to launch ccvault (Windows). Keep this window open while using it.
cd /d "%~dp0"
echo Starting ccvault ... your browser will open shortly.
echo (Keep this window open while using it; close it to stop.)
echo.
python ccvault.py
pause
