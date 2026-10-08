#Requires -Version 5.1
<#
.SYNOPSIS
    tsp-fresh 部署编排脚本的公共函数库。

.DESCRIPTION
    本文件被 scripts/deploy.ps1 与 scripts/rollback.ps1 通过 dot-source 引入,
    不要单独执行。

    提供的能力:
      1. 分级中文日志输出(步骤 / 成功 / 警告 / 失败 / 演练)
      2. 外部命令的统一调用: 打印命令、检查 $LASTEXITCODE、失败抛可读中文错误
      3. Docker / Docker Compose 可用性检测、Compose 镜像名解析
      4. 部署 tag 文件的读写(scripts/.last-deploy-tag / .prev-deploy-tag)
      5. "后端是否正在写数据" 检测(端口 / 运行容器 / data 目录新鲜度)
      6. HTTP 健康检查轮询
      7. 旧镜像 tag 清理

    兼容性: Windows PowerShell 5.1 与 PowerShell 7+ 均可运行, 不使用 7+ 专有语法
    (不使用 &&、||、??、三元运算符 ?: 等)。

.NOTES
    本文件必须以 UTF-8 with BOM 保存, 否则 Windows PowerShell 5.1 会按系统
    ANSI 代码页解析中文字符串, 导致输出乱码。
#>

# ---------------------------------------------------------------------------
# 日志输出
# ---------------------------------------------------------------------------

function Write-SectionMsg {
    <#
    .SYNOPSIS 输出一个分节标题。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ''
    Write-Host ('=== ' + $Message + ' ===') -ForegroundColor Cyan
}

function Write-StepMsg {
    <#
    .SYNOPSIS 输出 [n/m] 形式的步骤标题。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$Index,
        [Parameter(Mandatory = $true)][int]$Total,
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('[' + $Index + '/' + $Total + '] ' + $Message) -ForegroundColor Cyan
}

function Write-InfoMsg {
    <#
    .SYNOPSIS 输出普通提示信息。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('       ' + $Message) -ForegroundColor Gray
}

function Write-OkMsg {
    <#
    .SYNOPSIS 输出成功信息, 前缀 [OK]。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('  [OK] ' + $Message) -ForegroundColor Green
}

function Write-WarnMsg {
    <#
    .SYNOPSIS 输出警告信息, 前缀 [WARN]。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('[WARN] ' + $Message) -ForegroundColor Yellow
}

function Write-FailMsg {
    <#
    .SYNOPSIS 输出失败信息, 前缀 [FAIL]。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ('[FAIL] ' + $Message) -ForegroundColor Red
}

function Write-DryMsg {
    <#
    .SYNOPSIS 输出 DryRun 演练信息, 前缀 [DRY]。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host (' [DRY] ' + $Message) -ForegroundColor Magenta
}

# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------

function Format-CommandLine {
    <#
    .SYNOPSIS 把 exe + 参数数组格式化成可复制的命令行字符串。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments
    )
    $parts = @($Exe) + $Arguments
    $rendered = @()
    foreach ($part in $parts) {
        if ($part -match '[\s"]') {
            $rendered += ('"' + ($part -replace '"', '\\"') + '"')
        }
        else {
            $rendered += $part
        }
    }
    return ($rendered -join ' ')
}

function Invoke-ExternalCommand {
    <#
    .SYNOPSIS 执行外部命令, 输出实时透传; 非预期退出码时抛出中文错误。

    .PARAMETER AllowedExitCodes
        视为"成功"的退出码集合, 默认为 @(0)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description,
        [int[]]$AllowedExitCodes = @(0),
        [switch]$DryRun,
        [switch]$Silent
    )

    $cmdLine = Format-CommandLine -Exe $Exe -Arguments $Arguments
    if (-not $Silent) {
        Write-Host ('       $ ' + $cmdLine) -ForegroundColor DarkGray
    }

    if ($DryRun) {
        return 0
    }

    & $Exe @Arguments
    $exitCode = $LASTEXITCODE
    if (-not ($AllowedExitCodes -contains $exitCode)) {
        throw ('命令执行失败(退出码 ' + $exitCode + ') : ' + $cmdLine + "`n" +
               '       环节: ' + $Description)
    }
    return $exitCode
}

function Invoke-ExternalCapture {
    <#
    .SYNOPSIS 执行外部命令并捕获标准输出; 失败返回 $null, 成功返回字符串数组。

    .DESCRIPTION
        用于只读查询(git rev-parse / docker compose config --images / docker images 等)。
        在 -DryRun 下同样会真实执行(只读、无副作用), 便于演练时拿到真实的镜像名与 tag。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [switch]$Silent
    )

    if (-not $Silent) {
        Write-Host ('       $ ' + (Format-CommandLine -Exe $Exe -Arguments $Arguments)) -ForegroundColor DarkGray
    }

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $stdout = & $Exe @Arguments 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previous

    if ($exitCode -ne 0) {
        return $null
    }
    if ($null -eq $stdout) {
        return ,@()
    }
    # 注意: 用一元逗号包一层, 防止 PowerShell 把"只有一行输出"的数组拆成裸字符串,
    # 否则调用方 $rows[0] 会取到字符串首字符而不是第一行。
    return ,@($stdout)
}

# ---------------------------------------------------------------------------
# 项目路径 / 环境
# ---------------------------------------------------------------------------

function Get-TspProjectRoot {
    <#
    .SYNOPSIS 由脚本自身位置推导项目根目录, 并校验关键文件存在。

    .DESCRIPTION
        约定脚本位于 <项目根>/scripts/ 下, 因此项目根 = 脚本目录的父目录。
        这样从任意工作目录调用脚本都能正确定位项目。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ScriptDirectory
    )

    $root = Split-Path -Parent $ScriptDirectory
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw '无法由脚本位置推导项目根目录。'
    }

    $root = (Resolve-Path -LiteralPath $root -ErrorAction SilentlyContinue).ProviderPath
    if (-not $root) {
        throw ('项目根目录不存在: ' + (Split-Path -Parent $ScriptDirectory))
    }

    $composeFile = Join-Path $root 'docker-compose.yml'
    if (-not (Test-Path -LiteralPath $composeFile)) {
        throw ('未在项目根找到 docker-compose.yml, 推导出的根目录为: ' + $root)
    }

    return $root
}

function Get-ConfiguredPort {
    <#
    .SYNOPSIS 读取 .env 中的 PORT, 读取不到则回退到默认端口。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [int]$DefaultPort = 3018
    )

    $envPath = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        return $DefaultPort
    }

    $lines = Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue
    if ($null -eq $lines) {
        return $DefaultPort
    }
    foreach ($line in $lines) {
        if ($line -match '^\s*PORT\s*=\s*"?(\d{2,5})"?\s*$') {
            return [int]$Matches[1]
        }
    }
    return $DefaultPort
}

function Test-TspEnvFile {
    <#
    .SYNOPSIS 检查 .env 是否存在; 不存在时给出可执行的修复建议。

    .DESCRIPTION
        docker-compose.yml 使用 env_file: [.env] 且挂载 ./.env:/app/.env:ro,
        .env 缺失会导致 docker compose 直接报错退出, 因此这里作为硬性前置检查。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [switch]$DryRun
    )

    $envPath = Join-Path $ProjectRoot '.env'
    if (Test-Path -LiteralPath $envPath) {
        Write-OkMsg '找到 .env 文件(Compose env_file 与只读挂载均需要它)'
        return $true
    }

    $examplePath = Join-Path $ProjectRoot '.env.example'
    $hint = '请先创建 .env: Copy-Item .env.example .env , 然后按需填写 TICKFLOW_API_KEY / AI_API_KEY / AUTH_PASSWORD'
    if (-not (Test-Path -LiteralPath $examplePath)) {
        $hint = '请先手动创建 .env 文件(.env.example 也不存在)'
    }

    if ($DryRun) {
        Write-WarnMsg ('当前缺少 .env, 真实执行时会在本步骤中止。' + $hint)
        return $false
    }

    Write-FailMsg ('缺少 .env 文件, docker compose 会因 env_file 缺失而直接失败。')
    Write-InfoMsg $hint
    throw '前置检查未通过: 缺少 .env 文件。'
}

# ---------------------------------------------------------------------------
# Docker / Compose
# ---------------------------------------------------------------------------

function Test-DockerReady {
    <#
    .SYNOPSIS 检查 docker 命令与守护进程是否可用。
    #>
    [CmdletBinding()]
    param()

    if (-not (Get-Command 'docker' -ErrorAction SilentlyContinue)) {
        throw '未找到 docker 命令, 请确认 Docker Desktop 已安装且 docker 在 PATH 中。'
    }

    $version = Invoke-ExternalCapture -Exe 'docker' -Arguments @('version', '--format', '{{.Server.Version}}') -Silent
    if ($null -eq $version) {
        throw 'docker 守护进程不可用(请确认 Docker Desktop 已启动, 等待状态变为 Running 后重试)。'
    }

    $versionText = ''
    if ($version.Count -gt 0) {
        $versionText = [string]$version[0]
    }
    if ([string]::IsNullOrWhiteSpace($versionText)) {
        $versionText = '(版本号为空)'
    }
    Write-OkMsg ('Docker 可用, Server 版本: ' + $versionText.Trim())
    return $true
}

function Get-ComposeInvocation {
    <#
    .SYNOPSIS 检测可用的 Compose 调用方式。

    .DESCRIPTION
        优先使用 "docker compose"(Compose V2 插件); 若不可用则回退到独立命令
        "docker-compose"(Compose V1)。返回 @{ Exe = ...; Prefix = ... }。
    #>
    [CmdletBinding()]
    param()

    $plugin = Invoke-ExternalCapture -Exe 'docker' -Arguments @('compose', 'version') -Silent
    if ($null -ne $plugin) {
        $pluginText = ''
        if ($plugin.Count -gt 0) {
            $pluginText = [string]$plugin[0]
        }
        Write-OkMsg ('使用 docker compose 插件 ' + $pluginText.Trim())
        return @{ Exe = 'docker'; Prefix = @('compose') }
    }

    if (Get-Command 'docker-compose' -ErrorAction SilentlyContinue) {
        Write-OkMsg '使用独立命令 docker-compose (Compose V1)'
        return @{ Exe = 'docker-compose'; Prefix = @() }
    }

    throw '未检测到可用的 docker compose / docker-compose, 请升级 Docker Desktop 或安装 Compose 插件。'
}

function Invoke-ComposeCommand {
    <#
    .SYNOPSIS 统一执行 docker compose 子命令。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Invocation,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description,
        [switch]$DryRun
    )

    $all = @() + $Invocation.Prefix + $Arguments
    return (Invoke-ExternalCommand -Exe $Invocation.Exe -Arguments $all -Description $Description -DryRun:$DryRun)
}

function Invoke-ComposeCapture {
    <#
    .SYNOPSIS 统一执行只读的 docker compose 查询命令并捕获输出。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Invocation,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Arguments
    )

    $all = @() + $Invocation.Prefix + $Arguments
    return (Invoke-ExternalCapture -Exe $Invocation.Exe -Arguments $all -Silent)
}

function Get-ComposeImageName {
    <#
    .SYNOPSIS 解析 Compose 构建出的镜像名(不含 tag)。

    .DESCRIPTION
        优先用 "docker compose config --images" 拿到权威名称;
        该命令不可用(例如 .env 缺失、Compose V1)时, 按 Compose 的项目名规则
        (目录名小写 + 非法字符替换为 -)拼接 "<项目名>-app" 作为回退。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][hashtable]$Invocation,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [string]$ServiceName = 'app'
    )

    $rows = Invoke-ComposeCapture -Invocation $Invocation -Arguments @('config', '--images')
    $firstCandidate = $null
    if ($null -ne $rows) {
        foreach ($row in $rows) {
            $text = ([string]$row).Trim()
            if ([string]::IsNullOrWhiteSpace($text)) {
                continue
            }
            # 多 service 时优先取与 service 同名后缀的那一个
            if ($text -like ('*-' + $ServiceName)) {
                Write-InfoMsg ('由 docker compose config --images 解析到镜像名: ' + $text)
                return $text
            }
            if ($null -eq $firstCandidate) {
                $firstCandidate = $text
            }
        }
        if ($firstCandidate) {
            Write-InfoMsg ('由 docker compose config --images 解析到镜像名: ' + $firstCandidate)
            return $firstCandidate
        }
    }

    $directoryName = Split-Path -Leaf $ProjectRoot
    $projectName = ($directoryName.ToLowerInvariant() -replace '[^a-z0-9_-]', '-').Trim('-')
    if ([string]::IsNullOrWhiteSpace($projectName)) {
        $projectName = 'tsp-fresh'
    }
    $fallback = $projectName + '-' + $ServiceName
    Write-WarnMsg ('无法通过 docker compose config --images 解析镜像名, 按目录名回退为: ' + $fallback)
    return $fallback
}

# ---------------------------------------------------------------------------
# 部署 tag 状态文件
# ---------------------------------------------------------------------------

function Get-DeployTagPath {
    <#
    .SYNOPSIS 返回 tag 状态文件路径。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ScriptDirectory,
        [Parameter(Mandatory = $true)][ValidateSet('last', 'prev')][string]$Kind
    )

    if ($Kind -eq 'last') {
        return (Join-Path $ScriptDirectory '.last-deploy-tag')
    }
    return (Join-Path $ScriptDirectory '.prev-deploy-tag')
}

function Read-DeployTag {
    <#
    .SYNOPSIS 读取 tag 状态文件内容; 不存在或为空时返回 $null。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $null
    }
    $content = Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $content) {
        return $null
    }
    $text = (($content | Out-String).Trim())
    if ([string]::IsNullOrWhiteSpace($text)) {
        return $null
    }
    return $text
}

function Write-DeployTag {
    <#
    .SYNOPSIS 写入 tag 状态文件(DryRun 下只打印不落盘)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value,
        [switch]$DryRun
    )

    if ($DryRun) {
        Write-DryMsg ('写入 ' + $Path + ' = ' + $Value)
        return
    }
    Set-Content -LiteralPath $Path -Value $Value -Encoding UTF8 -ErrorAction Stop
    Write-InfoMsg ('已写入 ' + (Split-Path -Leaf $Path) + ' = ' + $Value)
}

function New-DeployTag {
    <#
    .SYNOPSIS 生成本次部署的镜像 tag。

    .DESCRIPTION
        优先使用 git 短 hash; 工作区有未提交改动时追加 "-dirty-<时间戳>",
        避免两次构建复用同一个 tag 导致回滚指向同一份镜像。
        取不到 git 信息时回退为 yyyyMMdd-HHmmss。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )

    $hash = $null
    if (Get-Command 'git' -ErrorAction SilentlyContinue) {
        $rows = Invoke-ExternalCapture -Exe 'git' -Arguments @('-C', $ProjectRoot, 'rev-parse', '--short', 'HEAD') -Silent
        if ($null -ne $rows -and $rows.Count -gt 0) {
            $candidate = ([string]$rows[0]).Trim()
            if (-not [string]::IsNullOrWhiteSpace($candidate)) {
                $hash = $candidate
            }
        }
    }

    if (-not $hash) {
        $fallback = (Get-Date -Format 'yyyyMMdd-HHmmss')
        Write-WarnMsg ('未取到 git 短 hash(可能不是 git 仓库), 使用时间戳 tag: ' + $fallback)
        return $fallback
    }

    $dirtyRows = Invoke-ExternalCapture -Exe 'git' -Arguments @('-C', $ProjectRoot, 'status', '--porcelain') -Silent
    $isDirty = ($null -ne $dirtyRows -and $dirtyRows.Count -gt 0)
    if ($isDirty) {
        return ($hash + '-dirty-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    }
    return $hash
}

function Get-GitSummary {
    <#
    .SYNOPSIS 收集 git 状态摘要, 仅用于提示, 不阻断部署。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )

    $summary = [ordered]@{
        Branch     = '(未知)'
        Hash       = '(未知)'
        DirtyCount = 0
        IsRepo     = $false
    }

    if (-not (Get-Command 'git' -ErrorAction SilentlyContinue)) {
        return $summary
    }

    $branchRows = Invoke-ExternalCapture -Exe 'git' -Arguments @('-C', $ProjectRoot, 'rev-parse', '--abbrev-ref', 'HEAD') -Silent
    if ($null -ne $branchRows -and $branchRows.Count -gt 0) {
        $summary.Branch = ([string]$branchRows[0]).Trim()
        $summary.IsRepo = $true
    }

    $hashRows = Invoke-ExternalCapture -Exe 'git' -Arguments @('-C', $ProjectRoot, 'rev-parse', '--short', 'HEAD') -Silent
    if ($null -ne $hashRows -and $hashRows.Count -gt 0) {
        $summary.Hash = ([string]$hashRows[0]).Trim()
    }

    $statusRows = Invoke-ExternalCapture -Exe 'git' -Arguments @('-C', $ProjectRoot, 'status', '--porcelain') -Silent
    if ($null -ne $statusRows) {
        $summary.DirtyCount = $statusRows.Count
    }

    return $summary
}

# ---------------------------------------------------------------------------
# "是否正在写数据" 检测
# ---------------------------------------------------------------------------

function Test-WriteInProgress {
    <#
    .SYNOPSIS 检测后端是否可能正在写数据(长任务进行中)。

    .DESCRIPTION
        三个信号, 命中任一即视为"可能在写":
          1) 服务端口处于监听状态(后端活着, 可能在跑后台任务)
          2) Compose 项目内有 running 状态的容器
          3) data 目录下的 *.log / *.lock 或一级子目录在 WindowSec 秒内被更新

        返回 @{ Busy = [bool]; Signals = [string[]] }。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [int]$Port = 3018,
        [int]$WindowSec = 60,
        [hashtable]$ComposeInvocation = $null
    )

    $signals = New-Object System.Collections.ArrayList

    # 信号 1: 端口监听
    $listening = $false
    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        if ($null -ne $connections) {
            $listening = $true
        }
    }
    catch {
        $listening = $false
    }
    if ($listening) {
        $null = $signals.Add(('端口 ' + $Port + ' 正在监听(后端服务在运行, 可能正在处理后台任务)'))
    }

    # 信号 2: Compose 项目里有运行中的容器
    if ($null -ne $ComposeInvocation) {
        $runningRows = Invoke-ComposeCapture -Invocation $ComposeInvocation -Arguments @('ps', '--status', 'running', '--format', '{{.Name}}')
        if ($null -ne $runningRows) {
            foreach ($row in $runningRows) {
                $name = ([string]$row).Trim()
                if (-not [string]::IsNullOrWhiteSpace($name)) {
                    $null = $signals.Add(('Compose 容器 ' + $name + ' 处于 running 状态'))
                    break
                }
            }
        }
    }

    # 信号 3: data 目录新鲜度
    $dataPath = Join-Path $ProjectRoot 'data'
    if (Test-Path -LiteralPath $dataPath) {
        $now = Get-Date
        $freshTargets = @()

        $logFiles = Get-ChildItem -LiteralPath $dataPath -Filter '*.log' -File -ErrorAction SilentlyContinue
        if ($null -ne $logFiles) {
            $freshTargets += @($logFiles)
        }
        $lockFiles = Get-ChildItem -LiteralPath $dataPath -Filter '*.lock' -File -ErrorAction SilentlyContinue
        if ($null -ne $lockFiles) {
            $freshTargets += @($lockFiles)
        }
        $subDirs = Get-ChildItem -LiteralPath $dataPath -Directory -ErrorAction SilentlyContinue
        if ($null -ne $subDirs) {
            $freshTargets += @($subDirs)
        }

        $newest = $null
        $newestName = ''
        foreach ($item in $freshTargets) {
            if ($null -eq $newest) {
                $newest = $item.LastWriteTime
                $newestName = $item.Name
                continue
            }
            if ($item.LastWriteTime -gt $newest) {
                $newest = $item.LastWriteTime
                $newestName = $item.Name
            }
        }

        if ($null -ne $newest) {
            $ageSec = ($now - $newest).TotalSeconds
            if ($ageSec -le $WindowSec) {
                $null = $signals.Add(('data 目录最近 ' + [int]$ageSec + ' 秒内有写入(最新: ' + $newestName + ')'))
            }
        }
    }

    return @{
        Busy    = ($signals.Count -gt 0)
        Signals = @($signals.ToArray())
    }
}

# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

function Test-HealthEndpoint {
    <#
    .SYNOPSIS 轮询健康检查端点直到成功或超时。

    .DESCRIPTION
        端点为 http://127.0.0.1:<port>/health(注意不是 /api/health)。
        最多等待 TimeoutSec 秒, 每 IntervalSec 秒探测一次。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSec = 90,
        [int]$IntervalSec = 3
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $attempt = 0

    while ((Get-Date) -lt $deadline) {
        $attempt = $attempt + 1
        $ok = $false
        try {
            $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                $ok = $true
            }
        }
        catch {
            $ok = $false
        }

        if ($ok) {
            Write-OkMsg ('健康检查通过: ' + $Url + ' (第 ' + $attempt + ' 次探测, 2xx)')
            return $true
        }

        Write-InfoMsg ('第 ' + $attempt + ' 次探测未通过, ' + $IntervalSec + ' 秒后重试 ...')
        Start-Sleep -Seconds $IntervalSec
    }

    return $false
}

# ---------------------------------------------------------------------------
# 镜像 tag 与清理
# ---------------------------------------------------------------------------

function ConvertTo-SortableTime {
    <#
    .SYNOPSIS 把 docker 的 CreatedAt 文本(如 "2026-10-07 23:43:00 +0800 CST")转成可排序的时间。
    #>
    [CmdletBinding()]
    param(
        [string]$Raw
    )

    if ([string]::IsNullOrWhiteSpace($Raw)) {
        return [datetime]::MinValue
    }

    $parts = ($Raw.Trim() -split '\s+')
    $text = $Raw
    if ($parts.Count -ge 2) {
        $text = $parts[0] + ' ' + $parts[1]
    }

    $parsed = [datetime]::MinValue
    $ok = [datetime]::TryParse(
        $text,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::None,
        [ref]$parsed)
    if ($ok) {
        return $parsed
    }
    return [datetime]::MinValue
}

function Remove-OldDeployImages {
    <#
    .SYNOPSIS 只保留本项目最近 Keep 个镜像 tag, 删除更老的(删除前先打印清单)。

    .DESCRIPTION
        - 只处理 Repository 等于目标镜像名的行, 绝不会误删其他项目的镜像
        - latest 与 <none> 不纳入统计, latest 永不删除
        - ProtectedTags 中的 tag(当前版本 / 上一版本)永不删除
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ImageName,
        [int]$Keep = 5,
        [string[]]$ProtectedTags = @(),
        [switch]$DryRun
    )

    $rows = Invoke-ExternalCapture -Exe 'docker' -Arguments @('images', '--format', '{{.Repository}}|{{.Tag}}|{{.CreatedAt}}|{{.ID}}') -Silent
    if ($null -eq $rows) {
        Write-WarnMsg '无法读取镜像列表, 跳过清理步骤。'
        return
    }

    $candidates = @()
    foreach ($row in $rows) {
        $parts = ([string]$row) -split '\|'
        if ($parts.Count -lt 4) {
            continue
        }
        $repo = $parts[0].Trim()
        $tag = $parts[1].Trim()
        if ($repo -ne $ImageName) {
            continue
        }
        if ($tag -eq 'latest' -or $tag -eq '<none>') {
            continue
        }
        $candidates += [pscustomobject]@{
            Tag       = $tag
            CreatedAt = $parts[2]
            ImageId   = $parts[3].Trim()
            SortKey   = (ConvertTo-SortableTime -Raw $parts[2])
        }
    }

    if ($candidates.Count -eq 0) {
        Write-InfoMsg ('未找到 ' + $ImageName + ' 的历史 tag, 无需清理。')
        return
    }

    $sorted = @($candidates | Sort-Object -Property SortKey -Descending)
    $keepSet = @()
    $dropSet = @()

    $keptCount = 0
    foreach ($item in $sorted) {
        if ($keptCount -lt $Keep) {
            $keepSet += $item
            $keptCount = $keptCount + 1
        }
        else {
            $dropSet += $item
        }
    }

    # 受保护的 tag 即使落在"更老"区间也不删
    $finalDrop = @()
    foreach ($item in $dropSet) {
        if ($ProtectedTags -contains $item.Tag) {
            continue
        }
        $finalDrop += $item
    }

    Write-InfoMsg ('当前 ' + $ImageName + ' 历史 tag 数: ' + $candidates.Count + ' 个, 保留最近 ' + $Keep + ' 个:')
    foreach ($item in $keepSet) {
        Write-InfoMsg ('  保留 ' + $ImageName + ':' + $item.Tag + '  (' + $item.CreatedAt + ')')
    }

    if ($finalDrop.Count -eq 0) {
        Write-OkMsg '没有需要删除的旧镜像。'
        return
    }

    Write-Host ''
    Write-WarnMsg ('准备删除以下 ' + $finalDrop.Count + ' 个旧镜像 tag:')
    foreach ($item in $finalDrop) {
        Write-Host ('       - ' + $ImageName + ':' + $item.Tag + '  (id=' + $item.ImageId + ', ' + $item.CreatedAt + ')') -ForegroundColor Yellow
    }

    foreach ($item in $finalDrop) {
        try {
            $null = Invoke-ExternalCommand -Exe 'docker' -Arguments @('rmi', ($ImageName + ':' + $item.Tag)) -Description ('清理旧镜像 ' + $ImageName + ':' + $item.Tag) -DryRun:$DryRun
            Write-OkMsg ('已删除 ' + $ImageName + ':' + $item.Tag)
        }
        catch {
            # 镜像被容器占用时 docker rmi 会失败, 这不影响部署结果
            Write-WarnMsg ('删除 ' + $ImageName + ':' + $item.Tag + ' 失败(可能仍在被容器使用), 已跳过: ' + $_.Exception.Message)
        }
    }
}

function Write-DeploySummary {
    <#
    .SYNOPSIS 打印部署/回滚结果摘要。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][string]$ImageName,
        [Parameter(Mandatory = $true)][string]$Tag,
        [Parameter(Mandatory = $true)][string]$HealthUrl
    )

    Write-Host ''
    Write-Host ('---------------- ' + $Title + ' ----------------') -ForegroundColor Cyan
    Write-Host ('  镜像      : ' + $ImageName + ':' + $Tag)
    Write-Host ('  容器名    : TickFlow_Stock_Panel')
    Write-Host ('  健康检查  : ' + $HealthUrl)
    Write-Host ('  回滚命令  : .\scripts\rollback.ps1')
    Write-Host ('  查看日志  : docker compose logs -f --tail 200')
    Write-Host '------------------------------------------------------------' -ForegroundColor Cyan
}
