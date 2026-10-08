#Requires -Version 5.1
<#
.SYNOPSIS
    把 Docker named volume（默认 tsp_parquet）导出为 tar 备份，并做完整性校验。

.DESCRIPTION
    用途: 在做任何会动到 Docker 数据盘的操作前（例如把 docker_data.vhdx 从 C 盘
    迁移到其它盘），先把 named volume 里的行情数据拉一份可回滚的 tar 备份。

    编排流程(共 6 步):
      [1/6] 前置检查: Docker 守护进程、volume 是否存在、备份目录、目标盘可用空间
      [2/6] 确定备份文件名(带时间戳)并打印本次计划
      [3/6] tar 流式导出: 起一次性 alpine 容器, 只读挂 volume, 挂备份目录,
            直接用容器内 tar 写盘 —— 不经过宿主机内存中转, 也不压缩
            (parquet 本身已是压缩格式, gzip 只会白白多花几分钟)
      [4/6] 校验: 备份文件大小与实际耗时
      [5/6] 校验: tar -tf 列出条目数(总数 / 文件数 / parquet 数)
      [6/6] 抽查: 从 tar 里解出 1 个 parquet, 用 Polars 读出行数

    可选的更强校验:
      -FullRowCheck 会把整个 tar 解到临时目录, 用 Polars 求 kline_daily_enriched
      的总行数并与 -ExpectedRows 对照。更慢(约几百 MB 落盘), 默认关闭。

.PARAMETER VolumeName
    要备份的 Docker volume 名, 默认 tsp_parquet。

.PARAMETER BackupDir
    备份输出目录, 默认 E:\tsp-backups。不存在时自动创建。

.PARAMETER ExpectedFiles
    期望的文件条目数(不含目录), 默认 24414。对不上只告警不阻断。

.PARAMETER ExpectedParquet
    期望的 parquet 条目数, 默认 21426。对不上只告警不阻断。

.PARAMETER ExpectedRows
    -FullRowCheck 时期望的 kline_daily_enriched 总行数, 默认 1320178。

.PARAMETER FullRowCheck
    开启整包解包 + Polars 总行数校验(慢)。

.PARAMETER KeepBackups
    保留最近几个备份, 0 表示不清理。默认 0。

.PARAMETER DryRun
    演练: 只打印将要执行的命令, 不产生备份文件。

.EXAMPLE
    .\scripts\export-parquet.ps1
    备份 tsp_parquet 到 E:\tsp-backups\tsp_parquet-yyyyMMdd-HHmmss.tar 并校验。

.EXAMPLE
    .\scripts\export-parquet.ps1 -DryRun
    演练, 只看要执行什么。

.EXAMPLE
    .\scripts\export-parquet.ps1 -FullRowCheck
    额外做整包解包 + 总行数对照(慢, 但校验最强)。

.EXAMPLE
    .\scripts\export-parquet.ps1 -BackupDir D:\backups -KeepBackups 3
    备份到 D 盘, 并只保留最近 3 份。

.NOTES
    兼容性: Windows PowerShell 5.1 / PowerShell 7+。
    必须在 **PowerShell** 里执行。若一定要在 Git Bash 里跑, 需先
    `export MSYS_NO_PATHCONV=1`, 否则容器内的 /src /dst 会被 MSYS 改写成
    PortableGit 的 Windows 路径而报 "can't change directory"。
    本文件必须以 UTF-8 with BOM 保存, 否则 PS 5.1 会把中文解析成乱码。
#>

[CmdletBinding()]
param(
    [string]$VolumeName = 'tsp_parquet',
    [string]$BackupDir = 'E:\tsp-backups',
    [int]$ExpectedFiles = 24414,
    [int]$ExpectedParquet = 21426,
    [int]$ExpectedRows = 1320178,
    [switch]$FullRowCheck,
    [int]$KeepBackups = 0,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

$scriptDirectory = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDirectory)) {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$projectRoot = Split-Path -Parent $scriptDirectory

$totalSteps = 6
$exitCode = 0
$tempWorkDir = $null

function Write-SectionMsg { param([string]$Message) Write-Host ''; Write-Host ('=== ' + $Message + ' ===') -ForegroundColor Cyan }
function Write-StepMsg { param([int]$Index, [int]$Total, [string]$Message) Write-Host ('[' + $Index + '/' + $Total + '] ' + $Message) -ForegroundColor Cyan }
function Write-InfoMsg { param([string]$Message) Write-Host ('       ' + $Message) -ForegroundColor Gray }
function Write-OkMsg { param([string]$Message) Write-Host ('  [OK] ' + $Message) -ForegroundColor Green }
function Write-WarnMsg { param([string]$Message) Write-Host ('[WARN] ' + $Message) -ForegroundColor Yellow }
function Write-FailMsg { param([string]$Message) Write-Host ('[FAIL] ' + $Message) -ForegroundColor Red }
function Write-DryMsg { param([string]$Message) Write-Host (' [DRY] ' + $Message) -ForegroundColor Magenta }

function Invoke-Cmd {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description,
        [switch]$Simulate
    )
    $rendered = @()
    foreach ($p in (@($Exe) + $Arguments)) {
        if ($p -match '[\s"]') { $rendered += ('"' + $p + '"') } else { $rendered += $p }
    }
    Write-Host ('       $ ' + ($rendered -join ' ')) -ForegroundColor DarkGray
    if ($Simulate) { return 0 }
    & $Exe @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw ('命令失败(退出码 ' + $code + ') : ' + ($rendered -join ' ') + "`n       环节: " + $Description)
    }
    return 0
}

function Invoke-Capture {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [switch]$Silent
    )
    if (-not $Silent) {
        Write-Host ('       $ ' + (($Exe + ' ' + ($Arguments -join ' ')).Trim())) -ForegroundColor DarkGray
    }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $stdout = & $Exe @Arguments 2>$null
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { return $null }
    if ($null -eq $stdout) { return ,@() }
    return ,@($stdout)
}

function Get-PythonForPolars {
    <#
    .SYNOPSIS 找到能 import polars 的 python: 优先 backend/.venv, 其次 uv, 再其次系统 python。
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $venvPython = Join-Path $Root 'backend\.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython) {
        $probe = Invoke-Capture -Exe $venvPython -Arguments @('-c', 'import polars;print(polars.__version__)') -Silent
        if ($null -ne $probe -and $probe.Count -gt 0 -and $probe[0]) {
            Write-InfoMsg ('Polars 解释器: backend\.venv (polars ' + ([string]$probe[0]).Trim() + ')')
            return $venvPython
        }
    }

    $sysPython = Get-Command 'python' -ErrorAction SilentlyContinue
    if ($sysPython) {
        $probe = Invoke-Capture -Exe 'python' -Arguments @('-c', 'import polars;print(polars.__version__)') -Silent
        if ($null -ne $probe -and $probe.Count -gt 0 -and $probe[0]) {
            Write-InfoMsg ('Polars 解释器: 系统 python (polars ' + ([string]$probe[0]).Trim() + ')')
            return 'python'
        }
    }

    return $null
}

try {
    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host ('  Docker volume 导出备份: ' + $VolumeName) -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host '  *** DryRun 演练模式: 不会产生备份文件 ***' -ForegroundColor Magenta
    }
    Write-Host ('  项目根目录: ' + $projectRoot) -ForegroundColor Cyan
    Write-Host ('  备份目录  : ' + $BackupDir) -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    # -------------------------------------------------------------------------
    # [1/6] 前置检查
    # -------------------------------------------------------------------------
    Write-SectionMsg '前置检查'
    Write-StepMsg -Index 1 -Total $totalSteps -Message 'Docker / volume / 备份目录 / 目标盘空间'

    if (-not (Get-Command 'docker' -ErrorAction SilentlyContinue)) {
        throw '未找到 docker 命令, 请确认 Docker Desktop 已安装。'
    }
    $serverVersion = Invoke-Capture -Exe 'docker' -Arguments @('version', '--format', '{{.Server.Version}}') -Silent
    if ($null -eq $serverVersion) {
        if ($DryRun) {
            Write-WarnMsg 'Docker 守护进程当前不可用; 演练模式下继续展示流程(真实执行会在本步中止)。'
        }
        else {
            throw 'Docker 守护进程不可用(Docker Desktop 未启动?)。备份必须借助容器读取 volume, 请先启动 Docker Desktop。'
        }
    }
    else {
        Write-OkMsg ('Docker 可用, Server 版本: ' + ([string]$serverVersion[0]).Trim())
    }

    $volRows = Invoke-Capture -Exe 'docker' -Arguments @('volume', 'inspect', $VolumeName, '--format', '{{.Name}}|{{.Mountpoint}}') -Silent
    if ($null -eq $volRows -or $volRows.Count -eq 0) {
        if ($DryRun) {
            Write-WarnMsg ('无法确认 volume ' + $VolumeName + ' 是否存在(守护进程不可用); 演练模式继续。')
        }
        else {
            throw ('找不到 volume: ' + $VolumeName + '。可用列表: docker volume ls')
        }
    }
    else {
        $volFields = ([string]$volRows[0]) -split '\|'
        Write-OkMsg ('volume ' + $volFields[0] + ' 存在, Mountpoint=' + $volFields[1])
    }

    if (-not (Test-Path -LiteralPath $BackupDir)) {
        if ($DryRun) {
            Write-DryMsg ('将创建备份目录: ' + $BackupDir)
        }
        else {
            New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
            Write-OkMsg ('已创建备份目录: ' + $BackupDir)
        }
    }
    else {
        Write-OkMsg ('备份目录已存在: ' + $BackupDir)
    }

    $driveLetter = ($BackupDir.Substring(0, 1)).ToUpperInvariant()
    $drive = Get-PSDrive -Name $driveLetter -ErrorAction SilentlyContinue
    if ($drive) {
        $freeGiB = [math]::Round($drive.Free / 1GB, 1)
        Write-InfoMsg ('目标盘 ' + $driveLetter + ': 可用 ' + $freeGiB + ' GiB')
        if ($freeGiB -lt 2) {
            throw ('目标盘可用空间不足 2 GiB, 拒绝备份: ' + $driveLetter + ':')
        }
    }

    $pythonExe = Get-PythonForPolars -Root $projectRoot
    if (-not $pythonExe) {
        Write-WarnMsg '未找到带 polars 的 python, 第 [6/6] 步的 Polars 抽查将跳过(其余校验照常)。'
    }

    # -------------------------------------------------------------------------
    # [2/6] 备份文件名与计划
    # -------------------------------------------------------------------------
    Write-SectionMsg '备份计划'
    Write-StepMsg -Index 2 -Total $totalSteps -Message '确定备份文件名'

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $tarName = $VolumeName + '-' + $stamp + '.tar'
    $tarPath = Join-Path $BackupDir $tarName
    # Docker 的 -v 主机路径: 统一成正斜杠, 避免反斜杠被当成转义
    $mountHost = $BackupDir -replace '\\', '/'
    $mountSpec = $mountHost + ':/dst'

    Write-InfoMsg ('备份文件  : ' + $tarPath)
    Write-InfoMsg ('宿主机挂载: ' + $mountSpec)
    Write-InfoMsg ('容器内命令: tar -cf /dst/' + $tarName + ' -C /src .   (不压缩)')

    # -------------------------------------------------------------------------
    # [3/6] tar 流式导出
    # -------------------------------------------------------------------------
    Write-SectionMsg '导出'
    Write-StepMsg -Index 3 -Total $totalSteps -Message '一次性容器 + 容器内 tar 直写备份盘'

    $dockerArgs = @(
        'run', '--rm',
        '-v', ($VolumeName + ':/src:ro'),
        '-v', $mountSpec,
        'alpine:latest',
        'tar', '-cf', ('/dst/' + $tarName), '-C', '/src', '.'
    )

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $null = Invoke-Cmd -Exe 'docker' -Arguments $dockerArgs -Description '导出 volume 到 tar' -Simulate:$DryRun
    $sw.Stop()
    $elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)

    if ($DryRun) {
        Write-DryMsg '演练模式: 未真正导出, 后续校验步骤跳过。'
        Write-Host ''
        Write-DryMsg '演练结束, 未产生任何备份文件。'
        exit 0
    }
    Write-OkMsg ('导出完成, 耗时 ' + $elapsedSec + ' 秒')

    # -------------------------------------------------------------------------
    # [4/6] 体积校验
    # -------------------------------------------------------------------------
    Write-SectionMsg '校验 1/3: 体积与耗时'
    Write-StepMsg -Index 4 -Total $totalSteps -Message '备份文件大小'

    if (-not (Test-Path -LiteralPath $tarPath)) {
        throw ('备份文件未生成: ' + $tarPath)
    }
    $tarItem = Get-Item -LiteralPath $tarPath
    $sizeMiB = [math]::Round($tarItem.Length / 1MB, 1)
    $sizeGiB = [math]::Round($tarItem.Length / 1GB, 2)
    Write-OkMsg ('备份文件: ' + $tarItem.Name)
    Write-InfoMsg ('大小: ' + $tarItem.Length + ' bytes = ' + $sizeMiB + ' MiB (' + $sizeGiB + ' GiB)')
    Write-InfoMsg ('耗时: ' + $elapsedSec + ' 秒')
    if ($tarItem.Length -lt 1MB) {
        throw '备份文件异常小(< 1 MiB), 导出很可能失败。'
    }

    # -------------------------------------------------------------------------
    # [5/6] 条目数校验
    # -------------------------------------------------------------------------
    Write-SectionMsg '校验 2/3: 条目数'
    Write-StepMsg -Index 5 -Total $totalSteps -Message 'tar -tf 列出条目'

    $entries = Invoke-Capture -Exe 'tar' -Arguments @('-tf', $tarPath) -Silent
    if ($null -eq $entries) {
        throw 'tar -tf 执行失败, 备份文件可能已损坏。'
    }
    $entryTotal = $entries.Count
    $fileEntries = @($entries | Where-Object { -not ($_.TrimEnd().EndsWith('/')) })
    $parquetEntries = @($entries | Where-Object { $_ -like '*.parquet' })
    $fileCount = $fileEntries.Count
    $parquetCount = $parquetEntries.Count

    Write-InfoMsg ('总条目数      : ' + $entryTotal + '  (文件 + 目录)')
    Write-InfoMsg ('文件条目数    : ' + $fileCount + '  (期望 ' + $ExpectedFiles + ')')
    Write-InfoMsg ('parquet 条目数: ' + $parquetCount + '  (期望 ' + $ExpectedParquet + ')')

    if ($fileCount -eq $ExpectedFiles) {
        Write-OkMsg ('文件数对上: ' + $fileCount)
    }
    else {
        Write-WarnMsg ('文件数与期望不一致: 实际 ' + $fileCount + ' vs 期望 ' + $ExpectedFiles + ' (差 ' + ($fileCount - $ExpectedFiles) + ')。不做任何删改, 请人工核对。')
    }
    if ($parquetCount -eq $ExpectedParquet) {
        Write-OkMsg ('parquet 数对上: ' + $parquetCount)
    }
    else {
        Write-WarnMsg ('parquet 数与期望不一致: 实际 ' + $parquetCount + ' vs 期望 ' + $ExpectedParquet + ' (差 ' + ($parquetCount - $ExpectedParquet) + ')。')
    }

    # -------------------------------------------------------------------------
    # [6/6] 抽查 + Polars
    # -------------------------------------------------------------------------
    Write-SectionMsg '校验 3/3: 抽查解压 + Polars 读'
    Write-StepMsg -Index 6 -Total $totalSteps -Message '解出 1 个 parquet 并读行数'

    $tempWorkDir = Join-Path $env:TEMP ('tsp_export_' + $stamp)
    New-Item -ItemType Directory -Path $tempWorkDir -Force | Out-Null

    $spotEntry = $null
    foreach ($e in $parquetEntries) {
        if ($e -like './kline_daily_enriched/*') { $spotEntry = $e; break }
    }
    if (-not $spotEntry -and $parquetEntries.Count -gt 0) { $spotEntry = $parquetEntries[0] }
    if (-not $spotEntry) {
        throw 'tar 里没有任何 *.parquet 条目, 备份内容异常。'
    }

    Write-InfoMsg ('抽查条目: ' + $spotEntry)
    $null = Invoke-Cmd -Exe 'tar' -Arguments @('-xf', $tarPath, '-C', $tempWorkDir, $spotEntry) -Description '解出抽查文件'
    $spotFile = Join-Path $tempWorkDir ($spotEntry -replace '/', '\')
    if (-not (Test-Path -LiteralPath $spotFile)) {
        throw ('解出的文件不存在: ' + $spotFile)
    }
    Write-OkMsg ('解压成功: ' + (Get-Item -LiteralPath $spotFile).Length + ' bytes')

    if ($pythonExe) {
        $pyScript = Join-Path $tempWorkDir 'read_rows.py'
        $pyCode = @'
import sys
import polars as pl
path = sys.argv[1]
rows = pl.scan_parquet(path).select(pl.len()).collect().item()
cols = pl.scan_parquet(path).collect_schema().names()
print("ROWS=" + str(rows))
print("COLS=" + str(len(cols)))
'@
        Set-Content -LiteralPath $pyScript -Value $pyCode -Encoding UTF8
        $rowsOut = Invoke-Capture -Exe $pythonExe -Arguments @($pyScript, $spotFile) -Silent
        if ($null -ne $rowsOut) {
            foreach ($line in $rowsOut) {
                if (([string]$line) -like 'ROWS=*') { Write-OkMsg ('Polars 读取成功, 行数 = ' + ([string]$line).Substring(5)) }
                if (([string]$line) -like 'COLS=*') { Write-InfoMsg ('列数 = ' + ([string]$line).Substring(5)) }
            }
        }
        else {
            Write-WarnMsg 'Polars 读取失败(解释器报错), 备份文件本身已通过条目数校验。'
        }
    }
    else {
        Write-WarnMsg '跳过 Polars 抽查(未找到带 polars 的 python)。'
    }

    # 可选: 整包解包求总行数
    if ($FullRowCheck) {
        Write-Host ''
        Write-WarnMsg '-FullRowCheck 已开启: 整包解包求 kline_daily_enriched 总行数(较慢) ...'
        $fullDir = Join-Path $tempWorkDir 'full'
        New-Item -ItemType Directory -Path $fullDir -Force | Out-Null
        $null = Invoke-Cmd -Exe 'tar' -Arguments @('-xf', $tarPath, '-C', $fullDir) -Description '整包解包'
        if ($pythonExe) {
            $pyScript2 = Join-Path $tempWorkDir 'sum_rows.py'
            $pyCode2 = @'
import glob
import os
import sys
import polars as pl
root = os.path.join(sys.argv[1], "kline_daily_enriched")
files = sorted(glob.glob(os.path.join(root, "**", "*.parquet"), recursive=True))
total = 0
for f in files:
    total += pl.scan_parquet(f).select(pl.len()).collect().item()
print("FILES=" + str(len(files)))
print("TOTAL=" + str(total))
'@
            Set-Content -LiteralPath $pyScript2 -Value $pyCode2 -Encoding UTF8
            $sumOut = Invoke-Capture -Exe $pythonExe -Arguments @($pyScript2, $fullDir) -Silent
            if ($null -ne $sumOut) {
                $gotTotal = -1
                foreach ($line in $sumOut) {
                    if (([string]$line) -like 'TOTAL=*') { $gotTotal = [int](([string]$line).Substring(6)) }
                    if (([string]$line) -like 'FILES=*') { Write-InfoMsg ('解包后 kline_daily_enriched 文件数 = ' + ([string]$line).Substring(6)) }
                }
                Write-InfoMsg ('总行数 = ' + $gotTotal + '  (期望 ' + $ExpectedRows + ')')
                if ($gotTotal -eq $ExpectedRows) { Write-OkMsg '总行数精确对上。' }
                else { Write-WarnMsg ('总行数不一致: 实际 ' + $gotTotal + ' vs 期望 ' + $ExpectedRows) }
            }
        }
    }

    # 可选: 保留最近 N 份备份
    if ($KeepBackups -gt 0) {
        Write-Host ''
        Write-InfoMsg ('清理策略: 只保留最近 ' + $KeepBackups + ' 份 ' + $VolumeName + '-*.tar')
        $old = @(Get-ChildItem -LiteralPath $BackupDir -Filter ($VolumeName + '-*.tar') -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending)
        if ($old.Count -gt $KeepBackups) {
            foreach ($f in ($old | Select-Object -Skip $KeepBackups)) {
                Write-WarnMsg ('删除旧备份: ' + $f.Name)
                Remove-Item -LiteralPath $f.FullName -Force
            }
        }
        else {
            Write-InfoMsg ('当前 ' + $old.Count + ' 份, 无需清理。')
        }
    }

    Write-Host ''
    Write-Host '---------------- 备份完成 ----------------' -ForegroundColor Cyan
    Write-Host ('  备份文件: ' + $tarPath)
    Write-Host ('  大小    : ' + $sizeMiB + ' MiB   耗时: ' + $elapsedSec + ' 秒')
    Write-Host ('  条目    : 总 ' + $entryTotal + ' / 文件 ' + $fileCount + ' / parquet ' + $parquetCount)
    Write-Host ('  恢复命令: docker run --rm -v ' + $VolumeName + ':/dst -v "' + $BackupDir + ':/src:ro" alpine tar -xf /src/' + $tarName + ' -C /dst')
    Write-Host '--------------------------------------------------' -ForegroundColor Cyan
}
catch {
    Write-Host ''
    Write-FailMsg ($_.Exception.Message)
    $exitCode = 1
}
finally {
    if ($tempWorkDir -and (Test-Path -LiteralPath $tempWorkDir)) {
        Remove-Item -LiteralPath $tempWorkDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-InfoMsg ('已清理临时目录: ' + $tempWorkDir)
    }
}

exit $exitCode
