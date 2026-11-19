@echo off
REM ==========================================================================
REM   MCSync - one-click push to GitHub
REM   Thin launcher: all logic lives in push.py (Chinese output handled by
REM   Python). Keeping this file pure ASCII avoids the cmd byte-truncation
REM   bug where multi-byte characters get split across the read buffer.
REM ==========================================================================
chcp 65001 >nul 2>nul
set PYTHONIOENCODING=utf-8

set "PYCMD=py -3"
where py >nul 2>nul
if errorlevel 1 set "PYCMD=python"

%PYCMD% "%~dp0push.py" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo [FAILED] exit code %RC%
pause
exit /b %RC%
