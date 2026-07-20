@echo off
REM ============================================================
REM  Sniper Bot - Control Deck (dashboard) launcher for Windows
REM  Keeps the local dashboard running and auto-restarts on crash.
REM  Open http://127.0.0.1:8787  (local only)
REM ============================================================
cd /d "%~dp0"
set PYTHONPATH=src
title Sniper Bot Dashboard

:loop
echo [%date% %time%] starting dashboard on http://127.0.0.1:8787 ...
uv run python -m dashboard
echo [%date% %time%] dashboard stopped (exit %errorlevel%). Restarting in 5s... Ctrl+C to quit.
timeout /t 5 /nobreak >nul
goto loop
