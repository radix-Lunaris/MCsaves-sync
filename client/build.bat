@echo off
chcp 65001 >nul 2>nul
setlocal
set PYTHONIOENCODING=utf-8
title MCSync Client Builder

REM All build logic lives in build.py.
REM This file only launches it, because cmd parsing of CJK text,
REM brackets and percent signs is fragile and caused silent hangs.

set "PYCMD=py -3"
where py >nul 2>nul
if errorlevel 1 set "PYCMD=python"

%PYCMD% "%~dp0build.py" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" echo   [FAILED] exit code %RC%
pause
exit /b %RC%
