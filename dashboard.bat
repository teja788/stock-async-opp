@echo off
REM ============================================================
REM  stock-async-opp - launch the Streamlit dashboard (Windows)
REM  Opens in your browser at http://localhost:8501
REM ============================================================
setlocal
if not exist ".venv\Scripts\streamlit.exe" (
    echo [dashboard] .venv or streamlit missing. Run setup.bat first.
    exit /b 1
)
".venv\Scripts\streamlit.exe" run dashboard.py
endlocal
