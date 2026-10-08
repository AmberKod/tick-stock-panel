#Requires -Version 5.1
<#
.SYNOPSIS
    tsp-fresh 一键部署: 宿主机改代码 -> 打包镜像 -> 部署 -> 健康检查 -> 失败自动回滚。

.DESCRIPTION
    编排流程(共 6 步):
      [1/6] 前置检查: Docker 可用性、Compose 可用性、.env 是否存在、
                      是否正在写数据(后台长任务)、git 状态摘要、可选跑测试子集
      [2/6] 生成镜像 tag 并记录到 scripts/.last-deploy-tag
            (写入前把上一个 tag 备份为 scripts/.prev-deploy-tag, 供回滚使用)
      [3/6] docker compose build 构建镜像, 并给构建产物打上本次 tag(同时保留 latest)
      [4/6] docker compose up -d 部署/重启服务
      [5/6] 轮询 http://127.0.0.1:<port>/health, 最多 90 秒; 失败则自动回滚到上一版
      [6/6] 清理旧镜像: 只保留本项目最近 5 个 tag

    重要说明:
      - 本脚本不会修改 Dockerfile / docker-compose.yml / .dockerignore, 也不会动 data/ 目录。
      - 健康检查路径是 /health(不是 /api/health)。
      - 由于 compose 里没有 image: 键, 脚本通过 docker compose config --images 动态解析
        镜像名(本项目为 tsp-fresh-app), 再用 docker tag 打版本标签。

.PARAMETER Force
    跳过"检测到正在写数据时默认中止"的保护, 强制继续部署。

.PARAMETER SkipBuild
    跳过构建, 只用现有镜像重启服务(改了 .env 等运行时配置时很有用)。

.PARAMETER NoRollback
    健康检查失败时不做自动回滚, 直接报错退出(便于保留现场排查)。

.PARAMETER DryRun
    演练模式: 只打印将要执行的命令, 不构建、不启动容器、不写任何文件。

.PARAMETER RunTests
    部署前先跑一次后端测试子集(默认不跑, 全量测试耗时很长)。

.PARAMETER TestFilter
    配合 -RunTests 使用的 pytest -k 过滤表达式, 默认 "not slow"。

.PARAMETER Port
    健康检查端口; 默认从 .env 的 PORT 读取, 读不到则用 3018。

.PARAMETER HealthTimeoutSec
    健康检查总超时秒数, 默认 90。

.PARAMETER HealthIntervalSec
    健康检查轮询间隔秒数, 默认 3。

.PARAMETER BusyWindowSec
    判定"正在写数据"的时间窗口秒数: data 目录在该窗口内被更新过即视为忙, 默认 60。

.PARAMETER KeepImages
    清理时保留最近多少个镜像 tag, 默认 5。

.EXAMPLE
    .\scripts\deploy.ps1 -DryRun
    演练一遍, 只打印命令, 不做任何实际变更。

.EXAMPLE
    .\scripts\deploy.ps1
    标准一键部署: 构建 -> 部署 -> 健康检查(失败自动回滚)。

.EXAMPLE
    .\scripts\deploy.ps1 -SkipBuild
    只重启服务, 不重新构建镜像。

.EXAMPLE
    .\scripts\deploy.ps1 -Force -RunTests
    忽略"正在写数据"警告, 并在构建前跑一次后端测试子集。

.NOTES
    兼容性: Windows PowerShell 5.1 / PowerShell 7+。
    本文件必须以 UTF-8 with BOM 保存, 否则 PS 5.1 会把中文解析成乱码。
#>

[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$SkipBuild,
    [switch]$NoRollback,
    [switch]$DryRun,
    [switch]$RunTests,
    [string]$TestFilter = 'not slow',
    [int]$Port = 0,
    [int]$HealthTimeoutSec = 90,
    [int]$HealthIntervalSec = 3,
    [int]$BusyWindowSec = 60,
    [int]$KeepImages = 5
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

$totalSteps = 6
$exitCode = 0
$projectRoot = $null
$composeInvocation = $null

try {
    $projectRoot = Get-TspProjectRoot -ScriptDirectory $scriptDirectory

    Write-Host ''
    Write-Host '============================================================' -ForegroundColor Cyan
    Write-Host '  tsp-fresh 一键部署 (改代码 -> 打包镜像 -> 部署)' -ForegroundColor Cyan
    if ($DryRun) {
        Write-Host '  *** DryRun 演练模式: 不会执行任何有副作用的操作 ***' -ForegroundColor Magenta
    }
    Write-Host ('  项目根目录: ' + $projectRoot) -ForegroundColor Cyan
    Write-Host '============================================================' -ForegroundColor Cyan

    Push-Location -LiteralPath $projectRoot

    # -------------------------------------------------------------------------
    # [1/6] 前置检查
    # -------------------------------------------------------------------------
    Write-SectionMsg '前置检查'
    Write-StepMsg -Index 1 -Total $totalSteps -Message '检查 Docker / Compose / .env / 数据写入状态'

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

    # --- 是否正在写数据 ---
    $busyCheck = Test-WriteInProgress -ProjectRoot $projectRoot -Port $healthPort -WindowSec $BusyWindowSec -ComposeInvocation $composeInvocation
    if ($busyCheck.Busy) {
        Write-Host ''
        Write-Host '************************************************************' -ForegroundColor Yellow
        Write-Host '*  警告: 检测到后端可能正在写数据 / 正在运行              *' -ForegroundColor Yellow
        Write-Host '************************************************************' -ForegroundColor Yellow
        foreach ($signal in $busyCheck.Signals) {
            Write-Host ('  * - ' + $signal) -ForegroundColor Yellow
        }
        Write-Host '  * 边写边构建/重启可能复制到半截文件或中断后台长任务。  *' -ForegroundColor Yellow
        Write-Host '************************************************************' -ForegroundColor Yellow

        if (-not $Force) {
            if ($DryRun) {
                Write-Host ''
                Write-DryMsg '真实执行时会在这一步中止(未加 -Force)。演练继续, 以便展示完整命令序列。'
            }
            else {
                Write-Host ''
                Write-FailMsg '已中止部署。确认安全后请加 -Force 参数重新执行。'
                Write-InfoMsg '提示: 可先用 docker compose logs --tail 50 或查看 data/backend.log 确认后台任务是否已结束。'
                exit 1
            }
        }
        else {
            Write-Host ''
            Write-WarnMsg '已指定 -Force, 忽略"正在写数据"警告, 继续部署。'
        }
    }
    else {
        Write-OkMsg '未检测到正在写数据的迹象。'
    }

    # --- git 状态摘要(仅提示) ---
    $gitSummary = Get-GitSummary -ProjectRoot $projectRoot
    if ($gitSummary.IsRepo) {
        Write-InfoMsg ('git 分支: ' + $gitSummary.Branch + ' , HEAD: ' + $gitSummary.Hash + ' , 未提交条目: ' + $gitSummary.DirtyCount + ' 个')
        if ($gitSummary.DirtyCount -gt 0) {
            Write-WarnMsg '工作区有未提交改动, 本次镜像 tag 会带 -dirty 后缀以便区分。'
        }
    }
    else {
        Write-WarnMsg '当前目录不是 git 仓库(或未安装 git), 镜像 tag 将使用时间戳。'
    }

    # --- 可选: 后端测试子集 ---
    if ($RunTests) {
        Write-Host ''
        Write-InfoMsg '按 -RunTests 要求, 部署前先跑后端测试子集 ...'
        $testArguments = @('run', 'pytest', 'backend/tests', '-q', '--no-header', '-p', 'no:cacheprovider', '-k', $TestFilter)
        $testExit = Invoke-ExternalCommand -Exe 'uv' -Arguments $testArguments -Description '后端测试子集' -AllowedExitCodes @(0, 5) -DryRun:$DryRun
        if ($testExit -eq 5) {
            Write-WarnMsg ('pytest 未收集到任何用例(-k "' + $TestFilter + '"), 视为通过。')
        }
        else {
            Write-OkMsg '后端测试子集通过。'
        }
    }
    else {
        Write-InfoMsg '未指定 -RunTests, 跳过测试(全量测试耗时较长)。'
    }

    # -------------------------------------------------------------------------
    # [2/6] 生成并记录镜像 tag
    # -------------------------------------------------------------------------
    Write-SectionMsg '镜像 tag'
    Write-StepMsg -Index 2 -Total $totalSteps -Message '生成本次部署的镜像 tag 并记录状态'

    $imageName = Get-ComposeImageName -Invocation $composeInvocation -ProjectRoot $projectRoot
    $newTag = New-DeployTag -ProjectRoot $projectRoot

    $lastTagPath = Get-DeployTagPath -ScriptDirectory $scriptDirectory -Kind 'last'
    $prevTagPath = Get-DeployTagPath -ScriptDirectory $scriptDirectory -Kind 'prev'

    $oldLastTag = Read-DeployTag -Path $lastTagPath
    $oldPrevTag = Read-DeployTag -Path $prevTagPath

    Write-InfoMsg ('镜像名      : ' + $imageName)
    Write-InfoMsg ('本次 tag    : ' + $newTag)
    Write-InfoMsg ('上一版 tag  : ' + $(if ($oldLastTag) { $oldLastTag } else { '(无, 首次部署)' }))

    # 轮换: 上一个 tag -> .prev-deploy-tag, 本次 tag -> .last-deploy-tag
    if ($oldLastTag) {
        Write-DeployTag -Path $prevTagPath -Value $oldLastTag -DryRun:$DryRun
    }
    else {
        Write-InfoMsg '没有历史 tag 记录(首次部署), .prev-deploy-tag 保持不变。'
    }
    Write-DeployTag -Path $lastTagPath -Value $newTag -DryRun:$DryRun

    # -------------------------------------------------------------------------
    # [3/6] 构建镜像
    # -------------------------------------------------------------------------
    Write-SectionMsg '构建镜像'
    Write-StepMsg -Index 3 -Total $totalSteps -Message 'docker compose build'

    if ($SkipBuild) {
        Write-WarnMsg '已指定 -SkipBuild, 跳过构建, 直接使用现有镜像。'
    }
    else {
        try {
            $null = Invoke-ComposeCommand -Invocation $composeInvocation -Arguments @('build') -Description '构建镜像' -DryRun:$DryRun

            # compose 文件里没有 image: 键, 构建产物固定是 <镜像名>:latest,
            # 这里补打本次版本 tag, 保留 latest 以便 compose 继续引用。
            $null = Invoke-ExternalCommand -Exe 'docker' -Arguments @('tag', ($imageName + ':latest'), ($imageName + ':' + $newTag)) -Description ('给镜像打版本 tag ' + $newTag) -DryRun:$DryRun
            Write-OkMsg ('镜像已打标签: ' + $imageName + ':' + $newTag + ' (同时保留 :latest)')
        }
        catch {
            # 构建失败: 把 tag 状态文件恢复到部署前, 避免下次回滚指向不存在的镜像
            Write-FailMsg ('构建失败: ' + $_.Exception.Message)
            Write-InfoMsg '正在回滚 tag 状态文件到部署前 ...'
            if ($oldLastTag) {
                Write-DeployTag -Path $lastTagPath -Value $oldLastTag -DryRun:$DryRun
            }
            if ($oldPrevTag) {
                Write-DeployTag -Path $prevTagPath -Value $oldPrevTag -DryRun:$DryRun
            }
            throw '构建阶段失败, 未改动任何运行中的服务。'
        }
    }

    # -------------------------------------------------------------------------
    # [4/6] 部署
    # -------------------------------------------------------------------------
    Write-SectionMsg '部署服务'
    Write-StepMsg -Index 4 -Total $totalSteps -Message 'docker compose up -d'

    $null = Invoke-ComposeCommand -Invocation $composeInvocation -Arguments @('up', '-d') -Description '启动/更新服务' -DryRun:$DryRun
    Write-OkMsg 'compose up -d 已执行。'

    if ($DryRun) {
        Write-DryMsg '演练模式: 后续健康检查与回滚均不会真实执行。'
        Write-DeploySummary -Title '部署演练完成' -ImageName $imageName -Tag $newTag -HealthUrl $healthUrl
        Write-Host ''
        Write-DryMsg '演练结束, 未做任何实际变更(未构建、未启动容器、未写文件)。'
        exit 0
    }

    # -------------------------------------------------------------------------
    # [5/6] 健康检查 + 失败自动回滚
    # -------------------------------------------------------------------------
    Write-SectionMsg '健康检查'
    Write-StepMsg -Index 5 -Total $totalSteps -Message ('轮询 ' + $healthUrl + ' (最多 ' + $HealthTimeoutSec + ' 秒)')

    $healthy = Test-HealthEndpoint -Url $healthUrl -TimeoutSec $HealthTimeoutSec -IntervalSec $HealthIntervalSec

    if ($healthy) {
        Write-OkMsg '部署成功, 服务已通过健康检查。'
    }
    else {
        Write-FailMsg ('健康检查在 ' + $HealthTimeoutSec + ' 秒内未通过。')

        $rollbackTarget = $null
        if ($oldLastTag) {
            $rollbackTarget = $oldLastTag
        }
        if ($NoRollback) {
            Write-WarnMsg '已指定 -NoRollback, 保留现场不回滚。'
            Write-InfoMsg '排查建议: docker compose logs --tail 200  或  docker compose ps'
            Write-InfoMsg ('如需手动回滚: .\scripts\rollback.ps1' + $(if ($rollbackTarget) { (' -Tag ' + $rollbackTarget) } else { '' }))
            throw '部署后健康检查失败(未回滚)。'
        }
        if (-not $rollbackTarget) {
            Write-FailMsg '没有可用的历史 tag(.prev-deploy-tag 为空, 可能是首次部署), 无法自动回滚。'
            Write-InfoMsg '排查建议: docker compose logs --tail 200'
            throw '部署后健康检查失败且无可用回滚目标。'
        }

        Write-Host ''
        Write-WarnMsg ('准备回滚到上一版镜像: ' + $imageName + ':' + $rollbackTarget)
        $null = Invoke-ExternalCommand -Exe 'docker' -Arguments @('tag', ($imageName + ':' + $rollbackTarget), ($imageName + ':latest')) -Description ('把 latest 指回上一版 ' + $rollbackTarget)
        $null = Invoke-ComposeCommand -Invocation $composeInvocation -Arguments @('up', '-d', '--no-build') -Description '使用上一版镜像重启服务'

        $rollbackHealthy = Test-HealthEndpoint -Url $healthUrl -TimeoutSec $HealthTimeoutSec -IntervalSec $HealthIntervalSec
        if ($rollbackHealthy) {
            Write-OkMsg ('已回滚到上一版 ' + $rollbackTarget + ' 并通过健康检查。')
            # 回滚成功后轮换状态: 上一版成为"当前", 失败的这一版成为"上一版"(便于向前滚回)
            Write-DeployTag -Path $prevTagPath -Value $newTag
            Write-DeployTag -Path $lastTagPath -Value $rollbackTarget
            Write-DeploySummary -Title '已回滚' -ImageName $imageName -Tag $rollbackTarget -HealthUrl $healthUrl
            Write-Host ''
            Write-WarnMsg ('本次新版本 ' + $newTag + ' 未通过健康检查, 请修复后重新执行部署。')
            exit 1
        }

        Write-FailMsg ('回滚到 ' + $rollbackTarget + ' 后健康检查依然失败, 服务可能处于不可用状态。')
        Write-InfoMsg '排查建议: docker compose logs --tail 200  /  docker compose ps  /  docker inspect TickFlow_Stock_Panel'
        throw '部署失败且自动回滚后仍未恢复。'
    }

    # -------------------------------------------------------------------------
    # [6/6] 清理旧镜像
    # -------------------------------------------------------------------------
    Write-SectionMsg '清理旧镜像'
    Write-StepMsg -Index 6 -Total $totalSteps -Message ('保留最近 ' + $KeepImages + ' 个 ' + $imageName + ' 的 tag')

    $protected = @($newTag)
    if ($oldLastTag) {
        $protected += $oldLastTag
    }
    Remove-OldDeployImages -ImageName $imageName -Keep $KeepImages -ProtectedTags $protected -DryRun:$DryRun

    Write-DeploySummary -Title '部署完成' -ImageName $imageName -Tag $newTag -HealthUrl $healthUrl
    Write-OkMsg '全部步骤执行完毕。'
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
