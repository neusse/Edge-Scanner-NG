@echo off
title Edge Scanner Replay
cd /d "%~dp0"
set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Project Python environment not found. Run setup.bat first.
    exit /b 1
)
"%PYTHON_EXE%" scripts\run_replay.py %*
echo.
echo === Replay exited ===
pause
