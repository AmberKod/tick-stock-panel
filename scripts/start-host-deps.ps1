# TSP 宿主依赖一键启动（手动运行, 不注册任何自启)
#
# 用途: 机器重启后, TSP 容器依赖的两个宿主服务没起来的话, 用这个脚本一次全起。
#   1. sec_relay  (端口 17897) - SEC EDGAR 中转(P0 美股财报用)
#   2. OpenBB API (端口 6900)  - 美股深度数据(P1 盘口/报表/filings 用)
# 不起它们: TSP 主功能(行情/热点/选股/回测)完全不受影响,
#           只有 /api/us/financials 和 /api/us/deep/* 会返回 available:false。
#
# 用法(手动, 需要时跑一次):
#   powershell -File E:\ai_codes\ai_personal_panel\tsp-fresh\scripts\start-host-deps.ps1
#   或在 PowerShell 里:  .\scripts\start-host-deps.ps1
#
# 幂等: 已在跑的服务会跳过。窗口保持打开(Ctrl+C 停止全部)。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot   # tsp-fresh/

function Test-PortListening([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

# --- 1. sec_relay (17897) ---
if (Test-PortListening 17897) {
    Write-Host "[OK]   sec_relay 已在 17897 运行, 跳过" -ForegroundColor Green
} else {
    Write-Host "[..]   启动 sec_relay (17897)..." -ForegroundColor Cyan
    $py = "C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    Start-Process -FilePath $py -ArgumentList "$root\scripts\sec_relay.py" -WindowStyle Minimized
    Start-Sleep -Seconds 2
    if (Test-PortListening 17897) {
        Write-Host "[OK]   sec_relay 已启动" -ForegroundColor Green
    } else {
        Write-Host "[XX]   sec_relay 启动失败, 手动检查: $py $root\scripts\sec_relay.py" -ForegroundColor Red
    }
}

# --- 2. OpenBB API (6900) ---
if (Test-PortListening 6900) {
    Write-Host "[OK]   OpenBB 已在 6900 运行, 跳过" -ForegroundColor Green
} else {
    Write-Host "[..]   启动 OpenBB API (6900, 首次加载约 20s)..." -ForegroundColor Cyan
    & "D:\OpenBB\start-openbb-api.ps1" -Host_ "0.0.0.0" -NoBrowser
}

# --- 验收 ---
Write-Host ""
Write-Host "=== 状态 ===" -ForegroundColor Yellow
foreach ($p in 17897, 6900) {
    $name = if ($p -eq 17897) { "sec_relay" } else { "OpenBB" }
    if (Test-PortListening $p) {
        Write-Host "  $name : $p  [运行中]" -ForegroundColor Green
    } else {
        Write-Host "  $name : $p  [未运行]" -ForegroundColor Red
    }
}
