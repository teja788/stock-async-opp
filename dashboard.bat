@echo off
REM ============================================================
REM  stock-async-opp - launch the Streamlit dashboard (Windows)
REM  Opens in your browser at http://localhost:8501
REM ============================================================
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\streamlit.exe" (
    echo [dashboard] .venv or streamlit missing. Run setup.bat first.
    pause
    exit /b 1
)
echo [dashboard] Launching... your browser will open at http://localhost:8501
".venv\Scripts\streamlit.exe" run dashboard.py
endlocal
