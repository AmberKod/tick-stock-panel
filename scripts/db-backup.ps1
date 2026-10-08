#Requires -Version 5.1
<#
.SYNOPSIS
    tsp-fresh Postgres 备份: pg_dump 导出 + D 盘异盘副本 + 保留策略 + 可恢复性校验。

.DESCRIPTION
    六步:
      [1/6] 预检 — docker 可用、db 容器 running 且 pg_isready 通过、.env 里 POSTGRES_* 齐备
      [2/6] 生成带时间戳的备份文件名
      [3/6] pg_dump 导出(默认 custom 格式 -Fc, 可被 pg_restore 直接识别)
      [4/6] 校验 — 文件非空 + pg_restore -l 能列出内容(证明不是坏档)
      [5/6] D 盘异盘副本 —— 关键: vhdx 现在位于 E 盘(Disk#1), 备份只放 E 等于
            同盘备份, 挡不住物理盘故障。D 盘在 Disk#0, 必须有一份。
      [6/6] 保留策略清理 + 打印恢复命令

    ⚠️ 二进制安全: pg_dump -Fc 输出的是二进制。PowerShell 5.1 的 `>` 重定向会把
       字节流按文本编码重新编码, 直接毁档。因此本脚本所有涉及 dump 的重定向都
       交给 cmd /c 做原生重定向。

    ⚠️ 认证: 容器内 pg_dump / pg_restore / psql 走 Unix socket, 官方镜像默认是
       trust 认证, 所以命令行里不会出现密码。若你改过 pg_hba, 本脚本会失败,
       届时请在 .env 里补 PGPASSWORD 并自行加 -e 传递。

    ⚠️ 实例归属: 默认目标容器 postgres-18.6 是用户自部署的**外部**实例(镜像
       postgres:18.6, 网络 ying-app-network, 宿主端口 15432), 不由本项目的
       docker-compose.yml 托管(compose 里的 db 服务已于 2026-10-08 移除)。
       本脚本只做只读查询与导出, 绝不停止 / 删除 / 重建该容器。

.EXAMPLE
    .\db-backup.ps1
    .\db-backup.ps1 -DryRun
    .\db-backup.ps1 -KeepBackups 30 -NoMirror
#>
[CmdletBinding()]
param(
    # 外部实例: 不再由 docker-compose.yml 托管, 容器名是实际运行的实例名。
    [string]$ContainerName = 'postgres-18.6',
    [string]$BackupDir = 'E:\tsp-backups\db',
    [string]$MirrorDir = 'D:\tsp-backups\db',
    [switch]$NoMirror,
    [int]$KeepBackups = 14,
    [ValidateSet('custom', 'plain')]
    [string]$Format = 'custom',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 公共库引入
$commonPath = Join-Path $PSScriptRoot 'deploy-common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) {
    throw ('未找到公共函数库: ' + $commonPath)
}
. $commonPath

# ------------------------------------------------------------ .env 读取helper
function Read-DotEnvValue {
    <#
    .SYNOPSIS 从项目根 .env 里读一个键的值(不去重、不展开、不报错)。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Key
    )

    $envFile = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw ('未找到 .env: ' + $envFile)
    }

    $value = $null
    foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$')) {
            $value = $Matches[1].Trim()
        }
    }
    return $value
}

# ============================================================ [0] 准备
Write-SectionMsg ('tsp-fresh Postgres 备份' + $(if ($DryRun) { ' (演练 -DryRun)' } else { '' }))

$projectRoot = Get-TspProjectRoot -ScriptDirectory $PSScriptRoot
Write-InfoMsg ('项目根: ' + $projectRoot)

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# ============================================================ [1/6] 预检
Write-StepMsg -Index 1 -Total 6 -Message '预检: docker / 容器 / .env'

if ($DryRun) {
    Write-DryMsg '演练模式: 只读查询照常执行, 写操作(导出/复制/删除)全部跳过。'
}

Test-DockerReady | Out-Null

# 只看 State.Status, 不再拼 Health.Status: 外部实例 postgres-18.6 没配 healthcheck,
# `{{.State.Health.Status}}` 会直接报 "map has no entry for key \"Health\"" 并让
# docker inspect 非零退出, 从而被误判成"容器不存在"(2026-10-08 首次真跑就挂在这)。
# 真正的存活性由下面的 pg_isready 把关, 比 healthcheck 更直接。
$inspect = Invoke-ExternalCapture -Exe 'docker' -Arguments @('inspect', '-f', '{{.State.Status}}', $ContainerName) -Silent
if ($null -eq $inspect -or $inspect.Count -eq 0) {
    throw ('未找到数据库容器: ' + $ContainerName + '。DB 现在是外部实例(postgres-18.6), 不由 docker-compose.yml 托管, 请确认该容器在跑; 不要试图 docker compose up -d db。')
}
$stateLine = [string]$inspect[0]
Write-OkMsg ('数据库容器 ' + $ContainerName + ' 状态: ' + $stateLine)
if ($stateLine -notmatch 'running') {
    throw ('数据库容器未在运行: ' + $stateLine)
}

# 校验用的 pg_restore 必须与容器内 pg_dump 同版本, 否则可能读不懂归档格式。
# 因此直接从运行中的容器反查镜像, 而不是写死版本号。
$dbImage = ''
$imgOut = Invoke-ExternalCapture -Exe 'docker' -Arguments @('inspect', '-f', '{{.Config.Image}}', $ContainerName) -Silent
if ($null -ne $imgOut -and $imgOut.Count -gt 0) { $dbImage = ([string]$imgOut[0]).Trim() }
if ([string]::IsNullOrWhiteSpace($dbImage)) { $dbImage = 'postgres:18.6' }
Write-InfoMsg ('校验用镜像: ' + $dbImage + ' (取自容器, 保证 pg_restore 与 pg_dump 同版本)')

$dbUser = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_USER'
$dbName = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_DB'
if ([string]::IsNullOrWhiteSpace($dbUser)) { throw '.env 缺少 POSTGRES_USER' }
if ([string]::IsNullOrWhiteSpace($dbName)) { throw '.env 缺少 POSTGRES_DB' }

# 宿主映射端口只在 .env 里有(不硬编码 5432)。本脚本走 docker exec(容器内 Unix
# socket), 不直接用这个端口, 但预检打印出来供人工核对: 端口对不上说明找错了实例。
$dbPort = '15432'
$dbPortRaw = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_PORT'
if (-not [string]::IsNullOrWhiteSpace($dbPortRaw)) {
    if ($dbPortRaw -notmatch '^[0-9]+$') {
        throw ('.env 的 POSTGRES_PORT 不是数字: ' + $dbPortRaw)
    }
    $dbPort = $dbPortRaw
}
else {
    Write-WarnMsg '.env 未设置 POSTGRES_PORT, 回落默认 15432(外部实例 postgres-18.6 的宿主映射端口)。'
}

Write-OkMsg ('连接参数: user=' + $dbUser + ' db=' + $dbName + ' hostPort=' + $dbPort + ' (密码不落命令行, 走 Unix socket)')

# 存活性闸门: 容器 running 不代表 Postgres 在接受连接, 必须真问一次。
$readyOut = Invoke-ExternalCapture -Exe 'docker' -Arguments @('exec', $ContainerName, 'pg_isready', '-U', $dbUser, '-d', $dbName) -Silent
if ($null -eq $readyOut -or $readyOut.Count -eq 0) {
    throw ('pg_isready 失败: 容器 ' + $ContainerName + ' 内的 Postgres 未接受连接(user=' + $dbUser + ', db=' + $dbName + ')。不备份一个连不上的库。')
}
Write-OkMsg ('pg_isready: ' + ([string]$readyOut[0]).Trim())

# ============================================================ [2/6] 文件名
Write-StepMsg -Index 2 -Total 6 -Message '生成备份文件名'

$ext = if ($Format -eq 'custom') { 'dump' } else { 'sql' }
$baseName = 'tsp-db-' + $dbName + '-' + $stamp + '.' + $ext
$outFile = Join-Path $BackupDir $baseName

if (-not (Test-Path -LiteralPath $BackupDir)) {
    if ($DryRun) {
        Write-DryMsg ('将创建目录: ' + $BackupDir)
    }
    else {
        New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
        Write-OkMsg ('已创建目录: ' + $BackupDir)
    }
}
else {
    Write-InfoMsg ('主备份目录: ' + $BackupDir)
}

if (-not $NoMirror) {
    Write-InfoMsg ('异盘副本目录: ' + $MirrorDir + ' (D 盘, Disk#0)')
}
else {
    Write-WarnMsg '已指定 -NoMirror, 本次不生成 D 盘异盘副本 —— 这违背数据安全红线, 仅在明确知道风险时使用。'
}

Write-InfoMsg ('备份文件: ' + $outFile)

# ============================================================ [3/6] 导出
Write-StepMsg -Index 3 -Total 6 -Message ('pg_dump 导出 (' + $Format + ' 格式)')

$dumpArgs = @('exec', $ContainerName, 'pg_dump', '-U', $dbUser)
if ($Format -eq 'custom') { $dumpArgs += @('-Fc') }
$dumpArgs += @('-d', $dbName)

$dumpCmd = 'docker ' + ($dumpArgs -join ' ') + ' > "' + $outFile + '"'
Write-Host ('       $ ' + $dumpCmd) -ForegroundColor DarkGray

if ($DryRun) {
    Write-DryMsg '演练: 跳过真实导出。'
}
else {
    & cmd /c $dumpCmd
    if ($LASTEXITCODE -ne 0) {
        throw ('pg_dump 失败(退出码 ' + $LASTEXITCODE + '): ' + $dumpCmd)
    }
    Write-OkMsg 'pg_dump 完成'
}

# ============================================================ [4/6] 校验
Write-StepMsg -Index 4 -Total 6 -Message '校验: 文件非空 + pg_restore 可识别'

if ($DryRun) {
    Write-DryMsg '演练: 跳过校验(文件尚未生成)。'
}
else {
    if (-not (Test-Path -LiteralPath $outFile)) {
        throw ('备份文件未生成: ' + $outFile)
    }
    $item = Get-Item -LiteralPath $outFile
    if ($item.Length -le 0) {
        throw ('备份文件为空(0 字节): ' + $outFile)
    }
    Write-OkMsg ('文件大小: ' + $item.Length + ' 字节 (' + [math]::Round($item.Length / 1MB, 2) + ' MiB)')

    if ($Format -eq 'custom') {
        # ⚠️ 踩过的真坑: PostgreSQL 16 的 pg_restore **不接受 "-" 表示 stdin** ——
        # 它会把 "-" 当成字面文件名, 报 "could not open input file "-"", 退出码 1。
        # 所以 `docker exec -i postgres-18.6 pg_restore -l - < file` 这种写法必然失败
        # (2026-10-08 首次真实演练就是在这里挂的)。
        # 正确做法: 起一个一次性容器, 把备份目录只读挂进去, 让 pg_restore 直接读文件路径。
        $mountSpec = $BackupDir + ':/tsp_dump_check:ro'
        $listCmd = 'docker run --rm -v "' + $mountSpec + '" --entrypoint pg_restore ' + $dbImage + ' -l /tsp_dump_check/' + $baseName
        Write-Host ('       $ ' + $listCmd) -ForegroundColor DarkGray
        $toc = & cmd /c $listCmd
        if ($LASTEXITCODE -ne 0) {
            throw ('pg_restore -l 校验失败(退出码 ' + $LASTEXITCODE + '): 备份文件可能已损坏。')
        }
        $tocCount = 0
        if ($null -ne $toc) { $tocCount = @($toc).Count }
        # pg_restore -l 退出码为 0 即证明归档结构可解析。条目数为 0 只说明库是空的,
        # 不是坏档 —— 所以这里是警告而不是 throw。
        if ($tocCount -le 0) {
            Write-WarnMsg 'pg_restore -l 未列出任何条目(目标库可能是空库); 归档结构本身可解析, 视为通过。'
        }
        else {
            Write-OkMsg ('pg_restore -l 识别通过, 目录 ' + $tocCount + ' 行(含注释行)')
        }
    }
    else {
        $head = Get-Content -LiteralPath $outFile -TotalCount 5 -Encoding UTF8
        $joined = ($head -join ' ')
        if ($joined -notmatch 'PostgreSQL database dump') {
            throw 'plain 格式备份头部未出现 "PostgreSQL database dump" 标记, 判定为坏档。'
        }
        Write-OkMsg 'plain 格式头部标记校验通过'
    }
}

# ============================================================ [5/6] 异盘副本
Write-StepMsg -Index 5 -Total 6 -Message '生成 D 盘异盘副本'

if ($NoMirror) {
    Write-WarnMsg '跳过(-NoMirror)。'
}
elseif ($DryRun) {
    Write-DryMsg ('演练: 将复制 ' + $outFile + ' -> ' + (Join-Path $MirrorDir $baseName))
}
else {
    if (-not (Test-Path -LiteralPath $MirrorDir)) {
        New-Item -ItemType Directory -Path $MirrorDir -Force | Out-Null
        Write-OkMsg ('已创建目录: ' + $MirrorDir)
    }
    $mirrorFile = Join-Path $MirrorDir $baseName
    Copy-Item -LiteralPath $outFile -Destination $mirrorFile -Force
    if (-not (Test-Path -LiteralPath $mirrorFile)) {
        throw ('异盘副本复制失败: ' + $mirrorFile)
    }
    $srcLen = (Get-Item -LiteralPath $outFile).Length
    $dstLen = (Get-Item -LiteralPath $mirrorFile).Length
    if ($srcLen -ne $dstLen) {
        throw ('异盘副本大小不一致: 源 ' + $srcLen + ' 字节, 副本 ' + $dstLen + ' 字节。')
    }
    Write-OkMsg ('异盘副本已生成且字节数一致: ' + $mirrorFile + ' (' + $dstLen + ' 字节)')
}

# ============================================================ [6/6] 保留策略
Write-StepMsg -Index 6 -Total 6 -Message ('保留策略: 每个目录保留最近 ' + $KeepBackups + ' 份')

$pattern = 'tsp-db-*.'
$dirsToClean = @($BackupDir)
if (-not $NoMirror) { $dirsToClean += $MirrorDir }

foreach ($dir in $dirsToClean) {
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    $files = @(Get-ChildItem -LiteralPath $dir -File -Filter ($pattern + '*') |
        Where-Object { $_.Extension -in @('.dump', '.sql') } |
        Sort-Object LastWriteTime -Descending)

    Write-InfoMsg ($dir + ' 现有 ' + $files.Count + ' 份')
    if ($files.Count -le $KeepBackups) {
        Write-InfoMsg '未超出保留上限, 不清理。'
        continue
    }

    $doomed = @($files | Select-Object -Skip $KeepBackups)
    foreach ($f in $doomed) {
        if ($DryRun) {
            Write-DryMsg ('将删除: ' + $f.FullName)
        }
        else {
            Remove-Item -LiteralPath $f.FullName -Force
            Write-InfoMsg ('已删除旧备份: ' + $f.Name)
        }
    }
}

# ============================================================ 收尾
Write-SectionMsg '备份完成'

if (-not $DryRun) {
    Write-Host ''
    Write-Host '恢复命令(照抄即可):' -ForegroundColor Cyan
    if ($Format -eq 'custom') {
        # 同样因为 pg_restore 不吃 stdin, 恢复必须先把归档放进容器再读文件路径。
        Write-Host '  # 1) 把归档放进容器' -ForegroundColor DarkGray
        Write-Host ('  docker cp "' + $outFile + '" ' + $ContainerName + ':/tmp/restore.dump') -ForegroundColor Gray
        Write-Host '  # 2) 恢复(--clean --if-exists 会先删同名对象, 可重复执行)' -ForegroundColor DarkGray
        Write-Host ('  docker exec ' + $ContainerName + ' pg_restore -U ' + $dbUser + ' -d ' + $dbName + ' --clean --if-exists /tmp/restore.dump') -ForegroundColor Gray
        Write-Host '  # 3) 清理容器内临时归档(别留在可写层里白占 vhdx)' -ForegroundColor DarkGray
        Write-Host ('  docker exec ' + $ContainerName + ' rm -f /tmp/restore.dump') -ForegroundColor Gray
    }
    else {
        Write-Host '  # 1) 放进容器' -ForegroundColor DarkGray
        Write-Host ('  docker cp "' + $outFile + '" ' + $ContainerName + ':/tmp/restore.sql') -ForegroundColor Gray
        Write-Host '  # 2) 用 -f 读文件(不走 stdin, 免得 PowerShell 重定向改编码)' -ForegroundColor DarkGray
        Write-Host ('  docker exec ' + $ContainerName + ' psql -U ' + $dbUser + ' -d ' + $dbName + ' -f /tmp/restore.sql') -ForegroundColor Gray
        Write-Host '  # 3) 清理' -ForegroundColor DarkGray
        Write-Host ('  docker exec ' + $ContainerName + ' rm -f /tmp/restore.sql') -ForegroundColor Gray
    }
    Write-Host ''
    Write-WarnMsg '恢复前请先确认目标库可被覆盖; 生产库恢复前务必再做一次当前状态备份。'
}
else {
    Write-DryMsg '演练结束, 未产生任何文件。'
}
