@echo off
setlocal
title TSP - Update Service (build image + run in Docker)

rem ============================================================
rem  One-click service update. Double-click me.
rem
rem  Pipeline (full detail lives in scripts\deploy.ps1):
rem    [1/6] Pre-check : docker / compose / .env / data-write busy check
rem    [2/6] Tag image : record this deploy tag so it can be rolled back
rem    [3/6] Build     : docker compose build
rem    [4/6] Deploy    : docker compose up -d --force-recreate
rem    [5/6] Health    : poll http://127.0.0.1:<port>/health
rem                      -> if it fails, auto rollback to previous image
rem    [6/6] Cleanup   : keep only the latest 5 image tags
rem
rem  NOTE on encoding: this .bat intentionally stays ASCII-only.
rem  Windows console defaults to codepage 936 (GBK), so any Chinese here
rem  risks mojibake. All Chinese output comes from the PowerShell scripts,
rem  which are saved as UTF-8 WITH BOM.
rem
rem  Advanced usage, run these in PowerShell / Terminal instead:
rem    powershell -File .\scripts\deploy.ps1 -DryRun      (rehearse, no changes)
rem    powershell -File .\scripts\deploy.ps1 -SkipBuild   (config-only change)
rem    powershell -File .\scripts\deploy.ps1 -Wait       (queue until background job ends)
rem    powershell -File .\scripts\deploy.ps1 -Force      (ignore busy warning, deploy now)
rem    powershell -File .\scripts\deploy.ps1 -RunTests    (run tests before build)
rem ============================================================

cd /d "%~dp0"

rem Windows does not provide HOME to double-clicked scripts, only USERPROFILE.
rem docker compose warns loudly about it on every step; set it so its $HOME-based
rem paths (docker config / buildx store) resolve correctly.
if not defined HOME set "HOME=%USERPROFILE%"

where docker >nul 2>nul || (
  echo [update-service] ERROR: docker not found in PATH.
  echo [update-service] Fix  : start Docker Desktop first, then try again.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\deploy.ps1" %*

if errorlevel 1 (
  echo.
  echo [update-service] Update FAILED. Please read the messages above.
  echo [update-service] Next: docker compose logs --tail 200
) else (
  echo.
  echo [update-service] Update succeeded.
)

echo.
pause
endlocal
