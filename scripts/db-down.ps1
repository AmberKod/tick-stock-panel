#Requires -Version 5.1
<#
.SYNOPSIS
    安全停止 tsp-fresh 的 Compose 服务 —— 绝不带 -v, 绝不删 named volume。

.DESCRIPTION
    三步:
      [1/3] 停止前快照: 列出将要停止的容器, 以及两个 external 卷的存在性
      [2/3] 执行 docker compose down(不带 -v)
      [3/3] 停止后校验: 卷必须还在

    🔴 为什么这个脚本必须存在:
        `docker compose down -v` 会连带删除 anonymous/named volume。我们的
        tsp_parquet(21,426 个 parquet / 1,320,178 行行情)与 tsp_dbdata 一旦被删,
        就是不可恢复的数据损失。
        两个卷都声明了 external: true, 理论上 `down -v` 也不会删它们 —— 但那是
        "应该", 不是"保证"。compose 版本差异、手滑多打一个参数、换个人来操作,
        都可能绕过这层保护。所以本脚本把"拒绝 -v"做成硬约束, 不靠人记住。

    🔴 本脚本对 -v 的态度:
        1. 脚本内部构造的命令里永远不出现 -v;
        2. 若调用方显式传了 -RemoveVolumes, 脚本直接报错退出(退出码 1), 不执行任何操作。

.EXAMPLE
    .\db-down.ps1
    .\db-down.ps1 -DryRun
    .\db-down.ps1 -RemoveVolumes     # 会被拒绝并退出 1
#>
[CmdletBinding()]
param(
    [switch]$RemoveVolumes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 公共库引入
$commonPath = Join-Path $PSScriptRoot 'deploy-common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) {
    throw ('未找到公共函数库: ' + $commonPath)
}
. $commonPath

# ============================================================ [0] 硬约束: 拒绝 -v
Write-SectionMsg ('tsp-fresh 停止服务' + $(if ($DryRun) { ' (演练 -DryRun)' } else { '' }))

if ($RemoveVolumes) {
    Write-Host ''
    Write-FailMsg '检测到 -RemoveVolumes。'
    Write-Host ''
    Write-Host '        本脚本拒绝执行任何带 -v 的 down。原因:' -ForegroundColor Red
    Write-Host '          · tsp_parquet 存着 21,426 个 parquet / 1,320,178 行行情数据;' -ForegroundColor Red
    Write-Host '          · tsp_dbdata 存着 Postgres 全部数据;' -ForegroundColor Red
    Write-Host '          · 两者删掉都不可恢复(备份只覆盖到最近一次导出)。' -ForegroundColor Red
    Write-Host ''
    Write-Host '        如果你确实需要删除卷, 请显式执行:' -ForegroundColor Yellow
    Write-Host '          docker volume rm tsp_parquet' -ForegroundColor Yellow
    Write-Host '          docker volume rm tsp_dbdata' -ForegroundColor Yellow
    Write-Host '        (这会永久销毁数据, 请自行承担后果)' -ForegroundColor Yellow
    Write-Host ''
    exit 1
}

$projectRoot = Get-TspProjectRoot -ScriptDirectory $PSScriptRoot
Write-InfoMsg ('项目根: ' + $projectRoot)

# 不改调用方的工作目录(否则从别处调用后会莫名其妙被"拽"到项目根);
# 改为给每条 compose 命令显式传 -f, 这样脚本对 cwd 无副作用。
$composeFile = Join-Path $projectRoot 'docker-compose.yml'

# ============================================================ [1/3] 停止前快照
Write-StepMsg -Index 1 -Total 3 -Message '停止前快照: 容器与卷'

if ($DryRun) {
    Write-DryMsg '演练模式: 只读查询照常执行, down 不真正执行。'
}

$compose = Get-ComposeInvocation

$psLines = Invoke-ExternalCapture -Exe $compose.Exe -Arguments (@($compose.Prefix) + @('-f', $composeFile, 'ps', '--format', '{{.Service}}|{{.Name}}|{{.Status}}')) -Silent
$svcCount = 0
if ($null -ne $psLines) {
    foreach ($l in $psLines) {
        if (-not [string]::IsNullOrWhiteSpace([string]$l)) {
            Write-InfoMsg ('运行中: ' + [string]$l)
            $svcCount++
        }
    }
}
if ($svcCount -eq 0) { Write-InfoMsg '当前没有运行中的服务。' }

$protectedVolumes = @('tsp_parquet', 'tsp_dbdata')
foreach ($v in $protectedVolumes) {
    $vInfo = Invoke-ExternalCapture -Exe 'docker' -Arguments @('volume', 'inspect', $v, '--format', '{{.Name}}') -Silent
    if ($null -eq $vInfo -or $vInfo.Count -eq 0) {
        Write-WarnMsg ('卷 ' + $v + ' 当前不存在(停止前)。')
    }
    else {
        Write-OkMsg ('卷 ' + $v + ' 存在(停止前)。')
    }
}

# ============================================================ [2/3] down
Write-StepMsg -Index 2 -Total 3 -Message '执行 docker compose down (不带 -v)'

$downArgs = @($compose.Prefix) + @('-f', $composeFile, 'down')
Write-Host ('       $ ' + $compose.Exe + ' ' + ($downArgs -join ' ')) -ForegroundColor DarkGray
Write-InfoMsg '注意: 上面这条命令里没有 -v, 卷不会被删除。'

if ($DryRun) {
    Write-DryMsg '演练: 跳过真实 down。'
}
else {
    & $compose.Exe @downArgs
    if ($LASTEXITCODE -ne 0) {
        throw ('docker compose down 失败(退出码 ' + $LASTEXITCODE + ')。')
    }
    Write-OkMsg '服务已停止。'
}

# ============================================================ [3/3] 停止后校验
Write-StepMsg -Index 3 -Total 3 -Message '停止后校验: 卷必须还在'

$missing = @()
foreach ($v in $protectedVolumes) {
    $vInfo = Invoke-ExternalCapture -Exe 'docker' -Arguments @('volume', 'inspect', $v, '--format', '{{.Name}}') -Silent
    if ($null -eq $vInfo -or $vInfo.Count -eq 0) {
        $missing += $v
        Write-FailMsg ('卷 ' + $v + ' 不见了! 请立即检查。')
    }
    else {
        Write-OkMsg ('卷 ' + $v + ' 仍在。')
    }
}

Write-SectionMsg '停止完成'

if ($missing.Count -gt 0) {
    Write-FailMsg ('以下卷缺失: ' + ($missing -join ', ') + ' —— 这不是本脚本造成的, 请立即排查并从备份恢复。')
    exit 1
}

Write-Host ''
Write-Host ('重新启动: ' + $compose.Exe + ' ' + (($compose.Prefix) -join ' ') + ' -f "' + $composeFile + '" up -d') -ForegroundColor Gray
Write-Host ('只起数据库: ' + $compose.Exe + ' ' + (($compose.Prefix) -join ' ') + ' -f "' + $composeFile + '" up -d db') -ForegroundColor Gray
Write-Host ''
Write-WarnMsg '再次强调: 任何时候都不要用 docker compose down -v。'
