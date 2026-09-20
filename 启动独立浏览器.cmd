@echo off
rem ===================================================================
rem  Start the ISOLATED Edge instance used for Zhihuishu login.
rem
rem  Double-click this file to launch it.
rem
rem  Why this exists:
rem    The daily Edge/Chrome window is already in use by something else.
rem    Chromium ties a running instance to its --user-data-dir, and a
rem    launch WITHOUT --user-data-dir always attaches to the existing
rem    instance. So the only way to get a truly separate window is a
rem    separate profile directory - which is what this does.
rem
rem    Parent process becomes explorer.exe when double-clicked, so the
rem    browser window survives after this console closes. (Launching it
rem    from an agent session would get it reaped.)
rem
rem  Usage:
rem      just double-click   -> opens the Zhihuishu login page
rem      pass extra flags    -> forwarded verbatim, e.g.  --port 9334
rem
rem  NOTE: pure ASCII + CRLF on purpose. A .cmd is parsed with the
rem  console code page, so non-ASCII text (even inside rem) can be
rem  corrupted. All Chinese output comes from Python instead.
rem ===================================================================
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

rem Python writes UTF-8; cmd defaults to cp936.
chcp 65001 >nul 2>nul

set "PY="
if exist "%ROOT%\.venv\Scripts\python.exe" set "PY=%ROOT%\.venv\Scripts\python.exe"
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
  echo         or create a .venv inside this project folder.
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   Isolated Edge instance for Zhihuishu
echo.
echo   This is NOT your daily browser window.
echo   This is NOT the Edge window another agent is using.
echo   Profile:  %ROOT%\.browser\edge-profile
echo   CDP port: 9333  (127.0.0.1 only)
echo ============================================================
echo.

"%PY%" -m orchestrator.browser --launch --url https://passport.zhihuishu.com/login %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo ============================================================
  echo   Next steps
  echo     1. Log in inside the Edge window that just opened.
  echo     2. Open another terminal in this folder and run:
  echo            zhs cookies_login
  echo        It watches that window and saves the cookies once you
  echo        are logged in. Leave this console open meanwhile.
  echo     3. Verify the session:
  echo            zhs cookies_verify
  echo.
  echo   To close the browser later: just close that window.
  echo   Never batch-kill msedge.exe - other agents are using it.
  echo ============================================================
) else (
  echo ============================================================
  echo   LAUNCH FAILED ^(exit code %RC%^)
  echo.
  echo   Run this and send me the output:
  echo     python -m orchestrator.browser --diagnose
  echo ============================================================
)
echo.
pause
