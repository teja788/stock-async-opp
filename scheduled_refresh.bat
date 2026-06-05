@echo off
REM ============================================================
REM  stock-async-opp - invoked by Windows Task Scheduler.
REM  Runs a catch-up refresh and appends output to a log.
REM  %~dp0 = this file's folder (the project root), so the task
REM  works regardless of the scheduler's working directory.
REM ============================================================
cd /d "%~dp0"
if not exist "runtime\logs" mkdir "runtime\logs"
if not exist ".venv\Scripts\python.exe" (
    echo [%date% %time%] .venv missing - run setup.bat >> "runtime\logs\schtask.log"
    exit /b 1
)
echo [%date% %time%] refresh start >> "runtime\logs\schtask.log"
".venv\Scripts\python.exe" -m scanner.cli refresh >> "runtime\logs\schtask.log" 2>&1
echo [%date% %time%] refresh done >> "runtime\logs\schtask.log"
