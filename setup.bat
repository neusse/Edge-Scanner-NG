@echo off
setlocal
title Edge Scanner NG Setup
cd /d "%~dp0"

echo Edge Scanner NG setup
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    goto :fail
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo Python 3.11 or newer is needed. This machine has:
    python --version
    goto :fail
)

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js was not found. Install the LTS version from https://nodejs.org/ and run setup.bat again.
    goto :fail
)

echo [1/5] Creating the Python environment in .venv
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/5] Installing the Python packages
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [3/5] Installing the dashboard packages (a few minutes the first time)
pushd dashboard-v2
call npm ci --no-audit --no-fund --loglevel=error
if errorlevel 1 (
    rem An older npm can reject a lock file written by a newer one; npm install reconciles it.
    echo       Retrying with npm install
    call npm install --no-audit --no-fund --loglevel=error
    if errorlevel 1 (
        popd
        goto :fail
    )
)

echo [4/5] Building the dashboard
call npm run build
if errorlevel 1 (
    popd
    goto :fail
)
popd

echo [5/5] Creating .env for your market-data keys
if exist ".env" (
    echo       .env already exists, left as it is
) else (
    copy ".env.example" ".env" >nul
    echo       Created. Open .env and add your Alpaca keys, or set DATA_PROVIDER=schwab.
)

echo.
echo Setup finished. Add your keys to .env, then start it with start_scanner.bat
echo and open http://localhost:7777
echo.
echo Every install starts empty: no watchlists, no saved screens of your own and no alert
echo history. The first start downloads market history and can take 10 to 20 minutes.
echo Optional sample setups: python scripts\install_setup_library.py
echo.
pause
exit /b 0

:fail
echo.
echo Setup stopped. Fix the problem above and run setup.bat again; finished steps are skipped or repeated safely.
pause
exit /b 1
