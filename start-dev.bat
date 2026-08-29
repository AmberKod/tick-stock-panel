@echo off
setlocal
title tick-stock-panel dev

rem ============================================================
rem  One-click dev launcher (double-click me)
rem  Starts backend :3018 (uvicorn --reload) + frontend :3011 (Vite)
rem  Ctrl-C in the window stops both.
rem
rem  pnpm 9.10.0 is installed at %LOCALAPPDATA%\pnpm9 and is
rem  injected into PATH here, so no global install is needed.
rem ============================================================

set "PNPM_BIN=%LOCALAPPDATA%\pnpm9\node_modules\.bin"
set "PATH=%PNPM_BIN%;%PATH%"

cd /d "%~dp0"

where pnpm >nul 2>nul || (
  echo [start-dev] ERROR: pnpm not found in %PNPM_BIN%
  echo [start-dev] fix: npm install --prefix "%%LOCALAPPDATA%%\pnpm9" pnpm@9.10.0
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0dev.ps1" %*

if errorlevel 1 (
  echo.
  echo [start-dev] dev.ps1 exited with an error. See messages above.
)
pause
endlocal
