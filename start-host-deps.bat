@echo off
setlocal
title TSP - Start Host Dependencies

rem ============================================================
rem  One-click starter for the two HOST-LEVEL services that TSP needs.
rem  Double-click me after a machine reboot.
rem
rem    1. sec_relay   port 17897  -> SEC EDGAR relay (US financial statements)
rem    2. OpenBB API  port 6900   -> US deep data (quote / statements / filings)
rem
rem  These must run on the HOST, not in Docker, because:
rem    - sec_relay bypasses an SNI-based block that hits the container's
rem      egress to data.sec.gov;
rem    - OpenBB needs the host proxy to reach yfinance / SEC.
rem
rem  Without them the main app is fully functional; only these endpoints
rem  fail-closed with available:false:
rem    GET /api/us/financials/{symbol}
rem    GET /api/us/deep/{quote,statement,filings,summary}/{symbol}
rem
rem  Idempotent: already-running services are skipped.
rem  Deliberately NOT registered as autostart, per user rule.
rem
rem  NOTE: ASCII-only on purpose (console codepage 936 / GBK).
rem  All Chinese output comes from the UTF-8-BOM PowerShell scripts.
rem ============================================================

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-host-deps.ps1" %*

echo.
pause
endlocal
