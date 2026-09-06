@echo off
REM Double-click this to start (or restart) the local-high stack:
REM scanner (Telegram alerts) + web dashboard + Cloudflare tunnel.
REM The dashboard URL is printed below and saved in logs\tunnel.log.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0scripts\start.ps1"
echo.
pause
