@echo off
title Scanner Live
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM The symbol list to scan. Without --universe, run_live.py uses
REM data/universe.csv and rebuilds it weekly with scripts/build_universe.py.
REM To scan a wider list, build it once and this script picks it up:
REM   python scripts/build_universe.py --out data/universe_all.csv
REM A file passed with --universe is used verbatim (no age check, no weekly
REM rebuild), so rebuild it by hand when its liquidity numbers get stale.
REM ---------------------------------------------------------------------------
set "UNIVERSE_ARG="
if exist "data\universe_all.csv" set "UNIVERSE_ARG=--universe data/universe_all.csv"

REM ---------------------------------------------------------------------------
REM Daily history, in CALENDAR days. 380 covers about 252 sessions, the longest
REM window anything reads (SMA 200, 52-week high/low). run_live.py alone
REM defaults to 60, which leaves those empty.
REM ---------------------------------------------------------------------------
set "HISTORY_DAYS=380"

set "PYTHON_EXE=.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Project Python environment not found. Run setup.bat first.
    exit /b 1
)

REM Keep the upstream schwabdev client and stream.  If this machine already
REM has a schwab-py JSON token, copy only its OAuth values into schwabdev's
REM native token database.  No second login or stream is created.
if not defined SCHWAB_APP_KEY if defined SCHWAB_API_KEY set "SCHWAB_APP_KEY=%SCHWAB_API_KEY%"
if defined SCHWAB_TOKEN_PATH "%PYTHON_EXE%" scripts\import_schwab_py_token.py
if errorlevel 1 exit /b 1

REM Anything passed to this script is appended, so extra flags still work:
REM   start_scanner.bat --log-level INFO
"%PYTHON_EXE%" scripts/run_live.py %UNIVERSE_ARG% --history-days %HISTORY_DAYS% %*

echo.
echo === Scanner exited ===
pause
