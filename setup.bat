@echo off
REM ============================================================
REM  stock-async-opp - one-time environment setup (Windows)
REM  Creates a local .venv and installs dependencies.
REM ============================================================
setlocal

echo [setup] Creating virtual environment in .venv ...
python -m venv .venv
if errorlevel 1 (
    echo [setup] ERROR: failed to create venv. Is Python 3.11+ on PATH?
    exit /b 1
)

echo [setup] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo [setup] Installing requirements ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [setup] ERROR: dependency install failed. See messages above.
    exit /b 1
)

echo [setup] Done. Try:  run.bat --help
endlocal
