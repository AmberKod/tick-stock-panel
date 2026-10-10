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

    # ---- 关键: 子进程必须"看不见终端" ------------------------------------
    # 事故复盘(2026-10-10): 双击 update-service.bat 时, 进程链是
    #   Explorer -> conhost -> cmd -> powershell -> docker compose
    # 在这条链上 compose 会把自己的 stdout 判定为终端(证据: 它的 logrus 输出切成了
    # 带 [NNNN] 偏移的着色格式 "WARN[0000] The \"HOME\" variable is not set."),
    # 随即去取控制台句柄, 而该句柄在此上下文里无效, 于是直接吐
    #   failed to get console: The handle is invalid.
    # 并以退出码 1 结束 —— 构建压根没开始, 秒失败。
    # 同一个命令在已终端化的常规 shell 里是好的, 所以这类故障很难自查。
    #
    # 对策: 三条标准流全部重定向 —— stdin 立刻 EOF, stdout/stderr 走管道。
    #       子进程里 isatty() 全为 false, compose 走非终端分支, 不再碰控制台。
    #       代价是要自己抽水防止管道缓冲区写满死锁(stderr 用异步读干净。
    # ----------------------------------------------------------------------
    $resolvedExe = $Exe
    $resolvedCommand = Get-Command $Exe -ErrorAction SilentlyContinue
    if ($null -ne $resolvedCommand -and $resolvedCommand.Source) {
        $resolvedExe = $resolvedCommand.Source
    }

    $argLine = New-Object System.Text.StringBuilder
    foreach ($argument in $Arguments) {
        $text = [string]$argument
        if ($argLine.Length -gt 0) {
            [void]$argLine.Append(' ')
        }
        if ($text -match '[\s"]') {
            $text = '"' + ($text -replace '"', '\"') + '"'
        }
        [void]$argLine.Append($text)
    }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $resolvedExe
    $psi.Arguments = $argLine.ToString()
    $psi.WorkingDirectory = (Get-Location).Path
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $child = New-Object System.Diagnostics.Process
    $child.StartInfo = $psi
    [void]$child.Start()

    # stdin 立刻关闭 -> 子进程 stdin 是 EOF 的管道而不是控制台
    try {
        $child.StandardInput.Close()
    }
    catch {
    }

    # stderr 异步抽干, 避免缓冲区写满 -> stdout 阻塞 -> 死锁
    $stderrTask = $child.StandardError.ReadToEndAsync()

    if ($Silent) {
        $stdoutText = $child.StandardOutput.ReadToEnd()
    }
    else {
        while ($true) {
            $line = $child.StandardOutput.ReadLine()
            if ($null -eq $line) {
                break
            }
            Write-Host $line
        }
    }

    $child.WaitForExit()
    $exitCode = $child.ExitCode
    $stderrText = ''
    try {
        $stderrText = $stderrTask.GetAwaiter().GetResult()
    }
    catch {
        $stderrText = ''
    }

    if (-not $Silent -and -not [string]::IsNullOrWhiteSpace($stderrText)) {
        Write-Host $stderrText
    }

    $global:LASTEXITCODE = $exitCode

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

    # 非终端三件套(2026-10-10 加): 见 Invoke-ExternalCommand 注释里的事故复盘。
    # 让 compose 从一开始就别把自己当成跑在终端里, 避免它去抓控制台句柄。
    $all = @() + $Invocation.Prefix + @('--progress', 'plain', '--ansi', 'never') + $Arguments
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

function Get-ActivePipelineJob {
    <#
    .SYNOPSIS 查询后端是否有正在跑的 pipeline 任务 —— "正在写数据"的权威证据。

    .DESCRIPTION
        GET /api/pipeline/jobs 返回 { active_id, jobs: [{ id, status, stage, ... }] }。
        active_id 非空、或 jobs 里有 status 属于 running/pending/queued 的, 即为活动任务。

        返回 @{ Known; ActiveId; ActiveJobs }。Known=$false 表示查询失败(端口没开/超时/解析不了),
        调用方按"未知"处理, 而不是当成"没任务"。
    #>
    [CmdletBinding()]
    param(
        [int]$Port = 3018,
        [int]$TimeoutSec = 5
    )

    $url = 'http://127.0.0.1:' + $Port + '/api/pipeline/jobs'

    $savedProxy      = [System.Net.WebRequest]::DefaultWebProxy
    $savedHttpProxy  = $env:HTTP_PROXY
    $savedHttpsProxy = $env:HTTPS_PROXY
    $savedAllProxy   = $env:ALL_PROXY

    try {
        [System.Net.WebRequest]::DefaultWebProxy = $null
        $env:HTTP_PROXY  = $null
        $env:HTTPS_PROXY = $null
        $env:ALL_PROXY   = $null

        $payload = Invoke-RestMethod -Uri $url -Method Get -TimeoutSec $TimeoutSec -ErrorAction Stop

        $activeId = ''
        try {
            if ($payload.active_id) { $activeId = [string]$payload.active_id }
        }
        catch {
            $activeId = ''
        }

        $activeJobs = New-Object System.Collections.ArrayList
        $liveStates = @('running', 'pending', 'queued', 'active', 'started')

        if ($payload.jobs) {
            foreach ($job in @($payload.jobs)) {
                $status = ''
                try { $status = ([string]$job.status).ToLowerInvariant() } catch { $status = '' }
                if ($liveStates -contains $status) {
                    $jobId = ''
                    try { $jobId = [string]$job.id } catch { $jobId = '' }
                    $stage = ''
                    try { $stage = [string]$job.stage } catch { $stage = '' }
                    $pct = ''
                    try { $pct = [string]$job.progress } catch { $pct = '' }
                    $null = $activeJobs.Add(@{ Id = $jobId; Status = $status; Stage = $stage; Pct = $pct })
                }
            }
        }

        return @{
            Known      = $true
            ActiveId   = $activeId
            ActiveJobs = @($activeJobs.ToArray())
        }
    }
    catch {
        return @{ Known = $false; ActiveId = ''; ActiveJobs = @() }
    }
    finally {
        [System.Net.WebRequest]::DefaultWebProxy = $savedProxy
        $env:HTTP_PROXY  = $savedHttpProxy
        $env:HTTPS_PROXY = $savedHttpsProxy
        $env:ALL_PROXY   = $savedAllProxy
    }
}

function Test-WriteInProgress {
    <#
    .SYNOPSIS 检测后端是否可能正在写数据(长任务进行中)。

    .DESCRIPTION
        分两级判定 —— 这是被真实使用纠过来的设计:

        [上下文信号, 仅展示, 不拦截]
          1) 服务端口处于监听状态
          2) Compose 项目内有 running 状态的容器
          >> 正常部署时这两条恒为真(服务本来就在跑), 若拿来拦截会导致每次更新都中止,
             等于把脚本废掉。故降级为纯信息。

        [写入证据, 命中任一即 Busy]
          3) 后端 /api/pipeline/jobs 报告有活动任务(active_id 非空 或 存在 running/pending/queued)
          4) data 目录下的 *.log / *.lock 或一级子目录在 WindowSec 秒内被更新

        返回 @{ Busy; BusySignals; ContextSignals }。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [int]$Port = 3018,
        [int]$WindowSec = 60,
        [hashtable]$ComposeInvocation = $null
    )

    # BusySignals  = 真正的写入证据 -> 会拦截部署
    # ContextSignals = 只是"服务在跑"的背景信息 -> 仅展示, 不拦截
    $busySignals = New-Object System.Collections.ArrayList
    $contextSignals = New-Object System.Collections.ArrayList

    # --- 上下文信号 1: 端口监听 ---
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
        $null = $contextSignals.Add(('端口 ' + $Port + ' 正在监听(服务在运行, 属正常现象)'))
    }

    # --- 上下文信号 2: Compose 项目里有运行中的容器 ---
    if ($null -ne $ComposeInvocation) {
        $runningRows = Invoke-ComposeCapture -Invocation $ComposeInvocation -Arguments @('ps', '--status', 'running', '--format', '{{.Name}}')
        if ($null -ne $runningRows) {
            foreach ($row in $runningRows) {
                $name = ([string]$row).Trim()
                if (-not [string]::IsNullOrWhiteSpace($name)) {
                    $null = $contextSignals.Add(('Compose 容器 ' + $name + ' 处于 running 状态'))
                    break
                }
            }
        }
    }

    # --- 写入证据 1(权威): 后端自报的活动 pipeline 任务 ---
    # 端口没监听时查询必然失败, Known=$false 属预期, 不算 Busy。
    if ($listening) {
        $jobProbe = Get-ActivePipelineJob -Port $Port -TimeoutSec 5
        if ($jobProbe.Known) {
            if (-not [string]::IsNullOrWhiteSpace($jobProbe.ActiveId)) {
                $null = $busySignals.Add(('后端有活动任务: active_id=' + $jobProbe.ActiveId))
            }
            foreach ($activeJob in $jobProbe.ActiveJobs) {
                $label = '后台任务 ' + $activeJob.Id + ' 状态=' + $activeJob.Status
                if (-not [string]::IsNullOrWhiteSpace($activeJob.Stage)) {
                    $label = $label + ' 阶段=' + $activeJob.Stage
                }
                if (-not [string]::IsNullOrWhiteSpace($activeJob.Pct)) {
                    $label = $label + ' 进度=' + $activeJob.Pct + '%'
                }
                $null = $busySignals.Add($label)
            }
        }
    }

    # --- 写入证据 2: data 目录新鲜度 ---
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
                $null = $busySignals.Add(('data 目录最近 ' + [int]$ageSec + ' 秒内有写入(最新: ' + $newestName + ')'))
            }
        }
    }

    return @{
        Busy           = ($busySignals.Count -gt 0)
        BusySignals    = @($busySignals.ToArray())
        ContextSignals = @($contextSignals.ToArray())
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

    # ------------------------------------------------------------------
    # 绕开系统代理(关键!)
    # 本机开了代理 7897(Clash), 而 Invoke-WebRequest / Invoke-RestMethod 默认会走
    # 系统代理去连 127.0.0.1:<port>, 代理对本地地址会返回假的 502
    # ("upstream connect failed")。后果: 明明服务已经起来了, 健康检查却判定失败,
    # deploy.ps1 会据此触发不必要的自动回滚 —— 把好镜像回滚成旧版本。
    # 等价问题在本仓其它地方的解法是 curl 加 --noproxy '*'; PowerShell 侧只能
    # 清空 DefaultWebProxy + 临时清掉 *_PROXY 环境变量(PS 5.1 没有 -NoProxy 参数)。
    # ------------------------------------------------------------------
    $savedProxy      = [System.Net.WebRequest]::DefaultWebProxy
    $savedHttpProxy  = $env:HTTP_PROXY
    $savedHttpsProxy = $env:HTTPS_PROXY
    $savedAllProxy   = $env:ALL_PROXY

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $attempt = 0

    try {
        [System.Net.WebRequest]::DefaultWebProxy = $null
        $env:HTTP_PROXY  = $null
        $env:HTTPS_PROXY = $null
        $env:ALL_PROXY   = $null

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
    }
    finally {
        [System.Net.WebRequest]::DefaultWebProxy = $savedProxy
        $env:HTTP_PROXY  = $savedHttpProxy
        $env:HTTPS_PROXY = $savedHttpsProxy
        $env:ALL_PROXY   = $savedAllProxy
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

function Test-LocalPortListening {
    <#
    .SYNOPSIS 检测本机某个 TCP 端口是否处于 Listen 状态。

    .DESCRIPTION
        优先用 Get-NetTCPConnection(速度快), 失败则退回 TcpClient 探活,
        保证在没有 NetTCPConnection cmdlet 的环境里也能给出结论。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$Port
    )

    try {
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return [bool]$connections
    }
    catch {
        $client = $null
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $connectTask = $client.ConnectAsync('127.0.0.1', $Port)
            $null = $connectTask.Wait(700)
            return [bool]$client.Connected
        }
        catch {
            return $false
        }
        finally {
            if ($client) {
                $client.Close()
                $client.Dispose()
            }
        }
    }
}

function Test-HttpEndpoint {
    <#
    .SYNOPSIS 对本地 HTTP 端点做一次快探(默认 5 秒超时), 同样绕开系统代理。

    .DESCRIPTION
        返回值 @{ Reachable; Status; Detail }。
        端口 Listen 不等于服务可用 —— 实测 OpenBB 可能端口在监听、但内部
        PermissionError 导致所有数据端点返回 500。所以这里要真探一次。
        invoke 失败时尽量把响应体截断读出来(FastAPI 会把异常放在 detail 字段),
        便于一眼定位而不是只看到"连不上"。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSec = 5
    )

    $savedProxy      = [System.Net.WebRequest]::DefaultWebProxy
    $savedHttpProxy  = $env:HTTP_PROXY
    $savedHttpsProxy = $env:HTTPS_PROXY
    $savedAllProxy   = $env:ALL_PROXY

    try {
        [System.Net.WebRequest]::DefaultWebProxy = $null
        $env:HTTP_PROXY  = $null
        $env:HTTPS_PROXY = $null
        $env:ALL_PROXY   = $null

        $response = Invoke-WebRequest -Uri $Url -Method Get -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        return [ordered]@{
            Reachable = $true
            Status    = [int]$response.StatusCode
            Detail    = ''
        }
    }
    catch {
        $detail = ''
        try {
            $errorResponse = $_.Exception.Response
            if ($errorResponse) {
                $stream = $errorResponse.GetResponseStream()
                if ($stream) {
                    $reader = New-Object System.IO.StreamReader($stream)
                    $body = $reader.ReadToEnd()
                    $reader.Close()
                    if ($body.Length -gt 260) { $body = $body.Substring(0, 260) }
                    $detail = $body
                }
            }
        }
        catch {
            $detail = ''
        }

        if (-not $detail) {
            $detail = $_.Exception.Message
            if ($detail.Length -gt 260) { $detail = $detail.Substring(0, 260) }
        }

        return [ordered]@{
            Reachable = $false
            Status    = 0
            Detail    = $detail
        }
    }
    finally {
        [System.Net.WebRequest]::DefaultWebProxy = $savedProxy
        $env:HTTP_PROXY  = $savedHttpProxy
        $env:HTTPS_PROXY = $savedHttpsProxy
        $env:ALL_PROXY   = $savedAllProxy
    }
}

function Get-RunningContainerImageId {
    <#
    .SYNOPSIS 取出某个容器当前运行所基于的镜像 ID(sha256), 容器不存在则返回空串。

    .DESCRIPTION
        用途: 首次部署(没有 .last-deploy-tag 历史)时, 把"此刻正在跑的那个镜像"
        锚定成一个可回滚的 tag。否则第一次使用本脚本时一旦健康检查失败,
        会因为拿不到回滚目标而只能保留现场人工排障。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ContainerName
    )

    try {
        $imageId = Invoke-ExternalCapture `
            -Exe 'docker' `
            -Arguments @('inspect', '-f', '{{.Image}}', $ContainerName)

        if ($null -ne $imageId -and $imageId.Count -gt 0) {
            return ([string]$imageId[0]).Trim()
        }
        return ''
    }
    catch {
        return ''
    }
}

function Get-HostDependencyStatus {
    <#
    .SYNOPSIS 检查 TSP 依赖的两个"宿主常驻服务"是否在跑且真的可用。

    .DESCRIPTION
        这两个服务跑在宿主机上而不是容器里(Docker 无法代劳), 机器重启后会掉。
        不跑它们 TSP 主体功能完全不受影响, 只有九章融合引入的美股数据接口会
        fail-closed 返回 available:false —— 所以要把它做成本次部署后的显式提示,
        而不是让用户自己去发现面板"不可用"再倒查。

        注: 端口 Listen 不足以判定可用 —— 实测出现过 OpenBB 端口正常监听、
        但内部 PermissionError 让所有数据端点返回 500 的情况(2026-10-09)。
        因此除了查端口, 还会对能探的 HTTP 端点做一次真探(self probe)。

        注意: 本函数只做检测与提示, 绝不代为启动(用户明确禁止注册任何自启,
        连带要求宿主依赖一律走手动幂等脚本)。
    #>
    [CmdletBinding()]
    param()

    $dependencies = @(
        [ordered]@{
            Name     = 'sec_relay (SEC EDGAR 中转)'
            Port     = 17897
            ProbeUrl = ''   # CONNECT 隧道, 无 HTTP index 可探, 只查端口
            Impact   = 'P0 美股财报 GET /api/us/financials/{symbol} 将返回 available:false'
            Starter  = 'scripts\sec_relay.py --upstream http://127.0.0.1:7897'
        },
        [ordered]@{
            Name     = 'OpenBB API (美股深度数据)'
            Port     = 6900
            ProbeUrl = 'http://127.0.0.1:6900/'
            Impact   = 'P1 深度盘口/报表/报送 GET /api/us/deep/* 将返回 available:false'
            Starter  = 'D:\OpenBB\start-openbb-api.ps1 -Host_ 0.0.0.0 -NoBrowser'
        }
    )

    foreach ($dependency in $dependencies) {
        $dependency.Listening = Test-LocalPortListening -Port $dependency.Port

        $dependency.Healthy = $false
        $dependency.Status  = '(未探)'
        $dependency.Detail  = ''

        if (-not $dependency.Listening) {
            $dependency.Status = '未运行'
            continue
        }

        if ([string]::IsNullOrWhiteSpace($dependency.ProbeUrl)) {
            $dependency.Healthy = $true
            $dependency.Status  = '端口在监听'
            continue
        }

        $probe = Test-HttpEndpoint -Url $dependency.ProbeUrl -TimeoutSec 5
        if ($probe.Reachable) {
            $dependency.Healthy = $true
            $dependency.Status  = ('自检 200 (HTTP ' + $probe.Status + ')')
        }
        else {
            $dependency.Healthy = $false
            $dependency.Status  = '端口在监听但自检失败'
            $dependency.Detail  = $probe.Detail
        }
    }

    return $dependencies
}

function Write-HostDependencyReport {
    <#
    .SYNOPSIS 打印宿主常驻依赖的状态报告 + 影响面与启动命令。
    #>
    [CmdletBinding()]
    param()

    $dependencies = Get-HostDependencyStatus

    Write-Host ''
    Write-Host '---------------- 宿主常驻依赖 ----------------' -ForegroundColor Cyan
    foreach ($dependency in $dependencies) {
        if ($dependency.Healthy) {
            Write-Host ('  [OK]   ' + $dependency.Name + ' : ' + $dependency.Port + '  (' + $dependency.Status + ')') -ForegroundColor Green
        }
        else {
            Write-Host ('  [缺失] ' + $dependency.Name + ' : ' + $dependency.Port + '  (' + $dependency.Status + ')') -ForegroundColor Yellow
            Write-Host ('         影响: ' + $dependency.Impact) -ForegroundColor Yellow
            Write-Host ('         启动: ' + $dependency.Starter) -ForegroundColor DarkGray
            if ($dependency.Detail) {
                Write-Host ('         自检返回: ' + $dependency.Detail) -ForegroundColor DarkGray
            }
            if ($dependency.Listening) {
                Write-Host '         注意: 端口在监听但服务自检不过, 通常是进程坏了 —— 需要结束进程后重新启动(见上方启动命令)。' -ForegroundColor Yellow
            }
        }
    }

    $missing = @($dependencies | Where-Object { -not $_.Healthy })
    if ($missing.Count -gt 0) {
        Write-Host ''
        Write-Host '  一次启动全部缺失依赖(幂等, 已在跑的会跳过):' -ForegroundColor Yellow
        Write-Host '    powershell -ExecutionPolicy Bypass -File .\scripts\start-host-deps.ps1' -ForegroundColor Cyan
        Write-Host '    或双击项目根目录的 start-host-deps.bat' -ForegroundColor Cyan
        Write-Host '  若某个服务"端口在监听但自检失败", 说明进程还活着却已坏掉 —— 上述幂等脚本会跳过它,' -ForegroundColor Yellow
        Write-Host '  需要先任务管理器结束对应 python / openbb-api 进程, 再重新运行。' -ForegroundColor Yellow
    }
    else {
        Write-Host '  全部宿主依赖均已就绪, 美股深度面板可正常使用。' -ForegroundColor Green
    }
    Write-Host '---------------------------------------------' -ForegroundColor Cyan

    return $dependencies
}
