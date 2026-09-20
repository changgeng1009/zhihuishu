@echo off
rem ===================================================================
rem  zhs - Command-line entry for the Zhihuishu automation platform.
rem
rem  Usage:   zhs <command> [options]
rem  Examples:
rem      zhs cookies_login
rem      zhs cookies_verify
rem      zhs list_courses
rem      zhs safety
rem      zhs --help
rem
rem  IMPORTANT - DO NOT DOUBLE-CLICK THIS FILE.
rem  To START THE BROWSER, double-click the other .cmd in this folder
rem  (its name is printed by the guide below).
rem
rem  NOTE: pure ASCII + CRLF on purpose. A .cmd is parsed with the
rem  console code page, so non-ASCII text (even inside rem) can be
rem  corrupted. All Chinese output comes from Python instead.
rem ===================================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "PY="
if not defined PY if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

if not defined PY (
  echo.
  echo [ERROR] Python interpreter not found.
  echo         Install Python 3.11+ and add it to PATH,
  echo         or create a .venv inside this project.
  echo.
  echo [ Press any key to close this window ]
  pause >nul
  exit /b 1
)

rem No arguments means this was almost certainly double-clicked.
if "%~1"=="" (
  cd /d "%ROOT%"
  "%PY%" -m orchestrator.cli
  echo [ Press any key to close this window ]
  echo.
  pause >nul
  exit /b 0
)

cd /d "%ROOT%"
"%PY%" -m orchestrator.cli %*
exit /b %ERRORLEVEL%
