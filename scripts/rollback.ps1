#Requires -Version 5.1
<#
.SYNOPSIS
    tsp-fresh 部署回滚: 把服务切回指定(或上一版)镜像, 并做健康检查。

.DESCRIPTION
    编排流程(共 4 步):
      [1/4] 前置检查: Docker / Compose / .env 可用性
      [2/4] 确定回滚目标 tag: 优先用 -Tag 参数, 否则读 scripts/.prev-deploy-tag
      [3/4] 把镜像的 latest 标签指向目标 tag, 然后 docker compose up -d --no-build 重启
      [4/4] 轮询 http://127.0.0.1:<port>/health, 报告回滚结果

    回滚成功后会轮换状态文件:
      - scripts/.last-deploy-tag 写入回滚后的 tag(当前实际运行的版本)
      - scripts/.prev-deploy-tag 写入回滚前的 tag(便于需要时再向前滚回)

    说明:
      - 本脚本不会修改 Dockerfile / docker-compose.yml / .dockerignore, 也不会动 data/ 目录。
      - 健康检查路径是 /health(不是 /api/health)。

.PARAMETER Tag
    要回滚到的镜像 tag。不传时读取 scripts/.prev-deploy-tag。

.PARAMETER DryRun
    演练模式: 只打印将要执行的命令, 不切换镜像、不重启容器、不写任何文件。

.PARAMETER Port
    健康检查端口; 默认从 .env 的 PORT 读取, 读不到则用 3018。

.PARAMETER HealthTimeoutSec
    健康检查总超时秒数, 默认 90。

.PARAMETER HealthIntervalSec
    健康检查轮询间隔秒数, 默认 3。

.EXAMPLE
    .\scripts\rollback.ps1 -DryRun
    演练回滚, 只打印将要执行的命令。

.EXAMPLE
    .\scripts\rollback.ps1
    回滚到 scripts/.prev-deploy-tag 记录的上一版。

.EXAMPLE
    .\scripts\rollback.ps1 -Tag e39c628
    回滚到指定 tag 的镜像。

.EXAMPLE
    .\scripts\rollback.ps1 -Tag e39c628 -Port 3018
    指定健康检查端口执行回滚。

.NOTES
    兼容性: Windows PowerShell 5.1 / PowerShell 7+。
    本文件必须以 UTF-8 with BOM 保存, 否则 PS 5.1 会把中文解析成乱码。
#>

[CmdletBinding()]
param(
    [string]$Tag = '',
    [switch]$DryRun,
    [int]$Port = 0,
    [int]$HealthTimeoutSec = 90,
    [int]$HealthIntervalSec = 3
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# ---------------------------------------------------------------------------
# 初始化: 引入公共库、定位项目根
# ---------------------------------------------------------------------------

$scriptDirectory = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($scriptDirectory)) {
    $scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$commonLibrary = Join-Path $scriptDirectory 'deploy-common.ps1'
if (-not (Test-Path -LiteralPath $commonLibrary)) {
    Write-Host '[FAIL] 未找到公共库 scripts/deploy-common.ps1, 无法继续。' -ForegroundColor Red
    exit 1
}
. $commonLibrary

$totalSteps = 4
$exitCode = 0
$projectRoot = $null

try {
    $projectRoot = Get-TspProjectRoot -ScriptDirectory $scriptDirectory

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host '  tsp-fresh 部署回滚' -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host '  *** DryRun 演练模式: 不会执行任何有副作用的操作 ***' -ForegroundColor Magenta
    }
    Write-Host ('  项目根目录: ' + $projectRoot) -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    Push-Location -LiteralPath $projectRoot

    # -------------------------------------------------------------------------
    # [1/4] 前置检查
    # -------------------------------------------------------------------------
    Write-SectionMsg '前置检查'
    Write-StepMsg -Index 1 -Total $totalSteps -Message '检查 Docker / Compose / .env'

    $null = Test-DockerReady
    $composeInvocation = Get-ComposeInvocation
    $null = Test-TspEnvFile -ProjectRoot $projectRoot -DryRun:$DryRun

    if ($Port -gt 0) {
        $healthPort = $Port
    }
    else {
        $healthPort = Get-ConfiguredPort -ProjectRoot $projectRoot -DefaultPort 3018
    }
    $healthUrl = 'http://127.0.0.1:' + $healthPort + '/health'
    Write-InfoMsg ('健康检查地址: ' + $healthUrl + '  (注意: 是 /health, 不是 /api/health)')

    $imageName = Get-ComposeImageName -Invocation $composeInvocation -ProjectRoot $projectRoot
    Write-InfoMsg ('镜像名: ' + $imageName)

    # -------------------------------------------------------------------------
    # [2/4] 确定回滚目标 tag
    # -------------------------------------------------------------------------
    Write-SectionMsg '回滚目标'
    Write-StepMsg -Index 2 -Total $totalSteps -Message '确定要回滚到的镜像 tag'

    $lastTagPath = Get-DeployTagPath -ScriptDirectory $scriptDirectory -Kind 'last'
    $prevTagPath = Get-DeployTagPath -ScriptDirectory $scriptDirectory -Kind 'prev'

    $currentTag = Read-DeployTag -Path $lastTagPath
    $prevTag = Read-DeployTag -Path $prevTagPath

    $targetTag = $Tag.Trim()
    $targetSource = '-Tag 参数'
    if ([string]::IsNullOrWhiteSpace($targetTag)) {
        $targetTag = $prevTag
        $targetSource = 'scripts/.prev-deploy-tag'
    }

    Write-InfoMsg ('当前记录版本(.last-deploy-tag) : ' + $(if ($currentTag) { $currentTag } else { '(无记录)' }))
    Write-InfoMsg ('上一版记录(.prev-deploy-tag)   : ' + $(if ($prevTag) { $prevTag } else { '(无记录)' }))

    if ([string]::IsNullOrWhiteSpace($targetTag)) {
        Write-FailMsg '无法确定回滚目标: -Tag 未指定且 scripts/.prev-deploy-tag 为空。'
        Write-InfoMsg '可先用 docker images 查看可用的历史 tag, 再用 -Tag 指定, 例如:'
        Write-InfoMsg '  docker images tsp-fresh-app --format "{{.Repository}}:{{.Tag}}"'
        Write-InfoMsg '  .\scripts\rollback.ps1 -Tag <某个历史 tag>'
        throw '回滚中止: 缺少回滚目标。'
    }

    Write-OkMsg ('回滚目标: ' + $imageName + ':' + $targetTag + '  (来源: ' + $targetSource + ')')

    # DryRun 下不校验镜像是否真实存在(避免演练失败), 真实执行前先校验
    if (-not $DryRun) {
        $imageIdRows = Invoke-ExternalCapture -Exe 'docker' -Arguments @('images', '-q', ($imageName + ':' + $targetTag)) -Silent
        $imageExists = ($null -ne $imageIdRows -and $imageIdRows.Count -gt 0 -and -not [string]::IsNullOrWhiteSpace(([string]$imageIdRows[0]).Trim()))
        if (-not $imageExists) {
            Write-FailMsg ('镜像 ' + $imageName + ':' + $targetTag + ' 不存在, 无法回滚。')
            Write-InfoMsg ('可用 tag 列表: docker images ' + $imageName + ' --format "{{.Tag}}"')
            throw '回滚中止: 目标镜像不存在。'
        }
        Write-OkMsg ('目标镜像存在(id: ' + (([string]$imageIdRows[0]).Trim()) + ')')
    }

    # -------------------------------------------------------------------------
    # [3/4] 切换镜像并重启
    # -------------------------------------------------------------------------
    Write-SectionMsg '切换镜像'
    Write-StepMsg -Index 3 -Total $totalSteps -Message '把 latest 指向目标 tag 并重启服务'

    $null = Invoke-ExternalCommand -Exe 'docker' -Arguments @('tag', ($imageName + ':' + $targetTag), ($imageName + ':latest')) -Description ('把 latest 指向 ' + $targetTag) -DryRun:$DryRun
    $null = Invoke-ComposeCommand -Invocation $composeInvocation -Arguments @('up', '-d', '--no-build') -Description '使用目标镜像重启服务(不重新构建)' -DryRun:$DryRun
    Write-OkMsg '服务已按目标镜像重启。'

    if ($DryRun) {
        Write-DeploySummary -Title '回滚演练完成' -ImageName $imageName -Tag $targetTag -HealthUrl $healthUrl
        Write-Host ''
        Write-DryMsg '演练结束, 未做任何实际变更(未切换镜像、未重启容器、未写文件)。'
        exit 0
    }

    # -------------------------------------------------------------------------
    # [4/4] 健康检查
    # -------------------------------------------------------------------------
    Write-SectionMsg '健康检查'
    Write-StepMsg -Index 4 -Total $totalSteps -Message ('轮询 ' + $healthUrl + ' (最多 ' + $HealthTimeoutSec + ' 秒)')

    $healthy = Test-HealthEndpoint -Url $healthUrl -TimeoutSec $HealthTimeoutSec -IntervalSec $HealthIntervalSec

    if ($healthy) {
        # 轮换状态: 回滚后的版本成为"当前", 回滚前的版本成为"上一版"
        Write-DeployTag -Path $prevTagPath -Value $(if ($currentTag) { $currentTag } else { $targetTag })
        Write-DeployTag -Path $lastTagPath -Value $targetTag
        Write-DeploySummary -Title '回滚完成' -ImageName $imageName -Tag $targetTag -HealthUrl $healthUrl
        Write-OkMsg ('已成功回滚到 ' + $imageName + ':' + $targetTag + ' 并通过健康检查。')
        exit 0
    }

    Write-FailMsg ('回滚到 ' + $targetTag + ' 后健康检查未通过。')
    Write-InfoMsg '排查建议: docker compose logs --tail 200  /  docker compose ps'
    Write-InfoMsg ('如需回到刚才的版本: .\scripts\rollback.ps1 -Tag ' + $(if ($currentTag) { $currentTag } else { '<当前版本 tag>' }))
    throw '回滚后健康检查失败。'
}
catch {
    Write-Host ''
    Write-FailMsg ($_.Exception.Message)
    $exitCode = 1
}
finally {
    if ($projectRoot) {
        Pop-Location -ErrorAction SilentlyContinue | Out-Null
    }
}

exit $exitCode
