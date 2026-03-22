@echo off
title Campaign Optimization Tool
cd /d "%~dp0"

REM Use current Python if available
python --version >nul 2>&1
if errorlevel 1 (
    echo Python not found. Please install Python and add it to PATH.
    pause
    exit /b 1
)

REM Install dependencies if needed (quiet)
pip install -r requirements.txt -q 2>nul

python app.py
if errorlevel 1 (
    echo.
    echo App exited with an error. See above.
    pause
)
