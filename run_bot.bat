@echo off
title MT5 FTMO Trading Bot
cd /d "%~dp0"
echo Starting MT5 FTMO Trading Bot...
echo Press Ctrl+C to stop.
echo.

:loop
"%~dp0.venv\Scripts\python.exe" main.py
echo.
echo Bot exited. Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
