#Requires -Version 5.1
<#
.SYNOPSIS
    Postgres 跨大版本升级: pg_dumpall → 新建卷/新版本容器 → restore → 校验 → 切换。

.DESCRIPTION
    🔴 为什么必须有这个脚本:
        Postgres 的数据目录格式**跨大版本不兼容**。把 postgres:16 的 PGDATA 直接
        挂给 postgres:17 起, 17 会拒绝启动并报 "database files are incompatible
        with server"。此时如果慌乱中删卷重来, 就是永久数据丢失。
        唯一安全的跨大版本路径是: 逻辑导出(pg_dumpall, 含 role 与全部 database)
        → 用新版本起一个**全新的空卷** → 逻辑导入 → 校验通过后再切换。

    七步:
      [1/7] 预检: docker、容器、当前版本、目标版本必须**大于**当前主版本
      [2/7] 强制备份: 调用 db-backup.ps1 先落一份(默认强制执行, 不可跳过)
      [3/7] pg_dumpall 全量逻辑导出(含 role / 全部 database / 表空间)
      [4/7] 创建新卷 tsp_dbdata_v<N>(旧卷全程不动, 可回滚)
      [5/7] 用目标版本镜像起新容器并 restore
      [6/7] 校验: 版本正确 + database 列表齐全 + 能查到数据
      [7/7] 切换: 默认只打印手工指令; 加 -AutoSwitch -Yes 才真的改 compose 并切

    🔴 安全边界:
        · 旧卷 tsp_dbdata **永远不被删除**, 所以任何一步失败都能回到原状态;
        · 备份是强制的(-SkipBackup 需要同时给 -Yes, 且会打印红字警告);
        · 切换默认不自动做, 需要显式 -AutoSwitch -Yes。

.PARAMETER TargetVersion
    目标版本, 形如 '17' 或 '17.2'。主版本号必须大于当前主版本号。

.EXAMPLE
    .\db-upgrade-major.ps1 -TargetVersion 17 -DryRun
    .\db-upgrade-major.ps1 -TargetVersion 17
    .\db-upgrade-major.ps1 -TargetVersion 17 -AutoSwitch -Yes
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TargetVersion,
    [string]$ContainerName = 'tsp_db',
    [string]$BackupDir = 'E:\tsp-backups\db',
    [string]$UpgradeDir = 'E:\tsp-backups\db-upgrade',
    [switch]$SkipBackup,
    [switch]$AutoSwitch,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 公共库引入
$commonPath = Join-Path $PSScriptRoot 'deploy-common.ps1'
if (-not (Test-Path -LiteralPath $commonPath)) {
    throw ('未找到公共函数库: ' + $commonPath)
}
. $commonPath

$backupScript = Join-Path $PSScriptRoot 'db-backup.ps1'

function Read-DotEnvValue {
    <#
    .SYNOPSIS 从项目根 .env 里读一个键的值。
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

# ============================================================ 开头
Write-SectionMsg ('Postgres 跨大版本升级 -> ' + $TargetVersion + $(if ($DryRun) { ' (演练 -DryRun)' } else { '' }))

$projectRoot = Get-TspProjectRoot -ScriptDirectory $PSScriptRoot
Write-InfoMsg ('项目根: ' + $projectRoot)

# 不改调用方的工作目录; 改为给 compose 显式传 -f, 脚本对 cwd 无副作用。
$composeFile = Join-Path $projectRoot 'docker-compose.yml'

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# ============================================================ [1/7] 预检
Write-StepMsg -Index 1 -Total 7 -Message '预检: docker / 容器 / 版本关系'

if ($DryRun) {
    Write-DryMsg '演练模式: 只读查询照常执行; 导出、建卷、起容器、切换全部跳过。'
}

Test-DockerReady | Out-Null

$inspect = Invoke-ExternalCapture -Exe 'docker' -Arguments @('inspect', '-f', '{{.State.Status}}', $ContainerName) -Silent
if ($null -eq $inspect -or $inspect.Count -eq 0) {
    throw ('未找到数据库容器: ' + $ContainerName)
}
Write-OkMsg ('数据库容器状态: ' + [string]$inspect[0])

$dbUser = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_USER'
$dbName = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_DB'
$dbPass = Read-DotEnvValue -ProjectRoot $projectRoot -Key 'POSTGRES_PASSWORD'
if ([string]::IsNullOrWhiteSpace($dbUser)) { throw '.env 缺少 POSTGRES_USER' }
if ([string]::IsNullOrWhiteSpace($dbName)) { throw '.env 缺少 POSTGRES_DB' }
if ([string]::IsNullOrWhiteSpace($dbPass)) { throw '.env 缺少 POSTGRES_PASSWORD(新容器初始化需要)' }

$curVerRaw = Invoke-ExternalCapture -Exe 'docker' -Arguments @('exec', $ContainerName, 'psql', '-U', $dbUser, '-d', 'postgres', '-tAc', 'SHOW server_version;') -Silent
if ($null -eq $curVerRaw -or $curVerRaw.Count -eq 0) {
    throw '无法读取当前 Postgres 版本。'
}
$currentVersion = ([string]$curVerRaw[0]).Trim()
$currentMajor = [int]($currentVersion.Split('.')[0])
Write-OkMsg ('当前版本: ' + $currentVersion + ' (主版本 ' + $currentMajor + ')')

$targetMajor = [int]($TargetVersion.Split('.')[0])
$targetImage = 'postgres:' + $TargetVersion
Write-InfoMsg ('目标版本: ' + $TargetVersion + ' (主版本 ' + $targetMajor + '), 镜像 ' + $targetImage)

if ($targetMajor -lt $currentMajor) {
    throw ('目标主版本 ' + $targetMajor + ' 低于当前 ' + $currentMajor + ': Postgres 不支持降级, 已中止。')
}
if ($targetMajor -eq $currentMajor) {
    Write-WarnMsg ('目标与当前同为主版本 ' + $currentMajor + ': 这不是跨大版本升级。')
    Write-Host '        小版本升级(如 16.9 -> 16.10)数据目录是兼容的, 直接改 compose 的' -ForegroundColor Yellow
    Write-Host '        image tag 后 docker compose up -d db 即可, 不需要本流程。已中止。' -ForegroundColor Yellow
    Write-Host ''
    exit 1
}
Write-OkMsg ('确认为跨大版本升级: ' + $currentMajor + ' -> ' + $targetMajor)

# ============================================================ [2/7] 强制备份
Write-StepMsg -Index 2 -Total 7 -Message '强制先备份(pg_dump)'

if ($SkipBackup) {
    if (-not $Yes) {
        Write-FailMsg '指定了 -SkipBackup 但没有 -Yes。跨大版本升级前跳过备份是高危操作, 必须同时给 -Yes 明示。'
        exit 1
    }
    Write-WarnMsg '已指定 -SkipBackup -Yes: 本次跳过备份。旧数据卷仍会保留, 但一旦导入过程出错将没有最近的可恢复档。'
}
else {
    if (-not (Test-Path -LiteralPath $backupScript)) {
        throw ('未找到备份脚本: ' + $backupScript)
    }
    Write-InfoMsg ('调用: ' + $backupScript)
    if ($DryRun) {
        Write-DryMsg '演练: 将以 -DryRun 调用 db-backup.ps1。'
        & $backupScript -DryRun
    }
    else {
        & $backupScript -BackupDir $BackupDir
        if ($LASTEXITCODE -ne 0) {
            throw ('备份失败(退出码 ' + $LASTEXITCODE + '), 已中止升级 —— 没有备份就不动数据。')
        }
    }
    Write-OkMsg '备份环节完成'
}

# ============================================================ [3/7] pg_dumpall
Write-StepMsg -Index 3 -Total 7 -Message 'pg_dumpall 全量逻辑导出(含 role / 全部 database)'

if (-not (Test-Path -LiteralPath $UpgradeDir)) {
    if ($DryRun) {
        Write-DryMsg ('将创建目录: ' + $UpgradeDir)
    }
    else {
        New-Item -ItemType Directory -Path $UpgradeDir -Force | Out-Null
        Write-OkMsg ('已创建目录: ' + $UpgradeDir)
    }
}

$dumpFile = Join-Path $UpgradeDir ('pgdumpall-v' + $currentMajor + '-to-v' + $targetMajor + '-' + $stamp + '.sql')
$dumpCmd = 'docker exec ' + $ContainerName + ' pg_dumpall -U ' + $dbUser + ' > "' + $dumpFile + '"'
Write-Host ('       $ ' + $dumpCmd) -ForegroundColor DarkGray

if ($DryRun) {
    Write-DryMsg '演练: 跳过真实导出。'
}
else {
    & cmd /c $dumpCmd
    if ($LASTEXITCODE -ne 0) {
        throw ('pg_dumpall 失败(退出码 ' + $LASTEXITCODE + ')。')
    }
    $sz = (Get-Item -LiteralPath $dumpFile).Length
    if ($sz -le 0) { throw ('pg_dumpall 产出 0 字节: ' + $dumpFile) }
    Write-OkMsg ('导出完成: ' + $dumpFile + ' (' + $sz + ' 字节)')

    $head = Get-Content -LiteralPath $dumpFile -TotalCount 3 -Encoding UTF8
    if (($head -join ' ') -notmatch 'PostgreSQL database cluster dump') {
        Write-WarnMsg '文件头部未出现 "PostgreSQL database cluster dump" 标记, 请人工确认内容。'
    }
    else {
        Write-OkMsg '头部标记校验通过'
    }
}

# ============================================================ [4/7] 新卷
Write-StepMsg -Index 4 -Total 7 -Message ('创建新卷 tsp_dbdata_v' + $targetMajor + ' (旧卷 tsp_dbdata 全程不动)')

$newVolume = 'tsp_dbdata_v' + $targetMajor
$newContainer = 'tsp_db_v' + $targetMajor

$existingVol = Invoke-ExternalCapture -Exe 'docker' -Arguments @('volume', 'inspect', $newVolume, '--format', '{{.Name}}') -Silent
if ($null -ne $existingVol -and $existingVol.Count -gt 0) {
    throw ('目标新卷已存在: ' + $newVolume + '。请确认是否上次升级残留, 手工处理后再跑, 本脚本不会覆盖已有卷。')
}

if ($DryRun) {
    Write-DryMsg ('将创建卷: ' + $newVolume)
}
else {
    & docker volume create $newVolume | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('创建卷失败: ' + $newVolume) }
    Write-OkMsg ('已创建卷: ' + $newVolume)
}
Write-InfoMsg ('旧卷 ' + 'tsp_dbdata' + ' 保留不动 —— 任何一步失败都能直接回到原状态。')

# ============================================================ [5/7] 新容器 + restore
Write-StepMsg -Index 5 -Total 7 -Message ('用 ' + $targetImage + ' 起新容器并 restore')

if ($DryRun) {
    Write-DryMsg ('将拉取并启动: ' + $newContainer + ' (镜像 ' + $targetImage + ', 卷 ' + $newVolume + ')')
    Write-DryMsg ('将 restore: docker exec -i ' + $newContainer + ' psql -U ' + $dbUser + ' -d postgres < "' + $dumpFile + '"')
}
else {
    Write-InfoMsg ('拉取镜像 ' + $targetImage + ' ...')
    & docker pull $targetImage
    if ($LASTEXITCODE -ne 0) { throw ('拉取镜像失败: ' + $targetImage) }

    $runArgs = @(
        'run', '-d',
        '--name', $newContainer,
        '-e', ('POSTGRES_USER=' + $dbUser),
        '-e', ('POSTGRES_PASSWORD=' + $dbPass),
        '-e', ('POSTGRES_DB=' + $dbName),
        '-e', 'TZ=Asia/Shanghai',
        '-v', ($newVolume + ':/var/lib/postgresql/data'),
        $targetImage
    )
    & docker @runArgs
    if ($LASTEXITCODE -ne 0) { throw ('启动新容器失败: ' + $newContainer) }
    Write-OkMsg ('新容器已启动: ' + $newContainer)

    Write-InfoMsg '等待新容器初始化(最多 60 秒)...'
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 2
        $chk = & docker exec $newContainer pg_isready -U $dbUser -d $dbName 2>$null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if (-not $ready) {
        throw ('新容器 ' + $newContainer + ' 在 60 秒内未就绪, 请检查 docker logs ' + $newContainer)
    }
    Write-OkMsg '新容器已就绪'

    $restoreCmd = 'docker exec -i ' + $newContainer + ' psql -U ' + $dbUser + ' -d postgres < "' + $dumpFile + '"'
    Write-Host ('       $ ' + $restoreCmd) -ForegroundColor DarkGray
    & cmd /c $restoreCmd
    if ($LASTEXITCODE -ne 0) {
        Write-WarnMsg 'restore 退出码非 0。pg_dumpall -> psql 常见有可忽略的报错(如已存在的 role), 请人工核对下方校验结果。'
    }
    else {
        Write-OkMsg 'restore 完成'
    }
}

# ============================================================ [6/7] 校验
Write-StepMsg -Index 6 -Total 7 -Message '校验新容器'

if ($DryRun) {
    Write-DryMsg '演练: 跳过校验。'
}
else {
    $nv = Invoke-ExternalCapture -Exe 'docker' -Arguments @('exec', $newContainer, 'psql', '-U', $dbUser, '-d', 'postgres', '-tAc', 'SHOW server_version;') -Silent
    if ($null -eq $nv -or $nv.Count -eq 0) { throw '无法读取新容器版本。' }
    $newVer = ([string]$nv[0]).Trim()
    Write-OkMsg ('新容器版本: ' + $newVer)

    $dbs = Invoke-ExternalCapture -Exe 'docker' -Arguments @('exec', $newContainer, 'psql', '-U', $dbUser, '-d', 'postgres', '-tAc', "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY 1;") -Silent
    $dbList = @()
    if ($null -ne $dbs) {
        foreach ($d in $dbs) {
            if (-not [string]::IsNullOrWhiteSpace([string]$d)) { $dbList += ([string]$d).Trim() }
        }
    }
    Write-OkMsg ('数据库列表: ' + ($dbList -join ', '))
    if ($dbList -notcontains $dbName) {
        throw ('新容器缺少目标库 ' + $dbName + ', 判定升级失败。旧卷仍在, 可直接回退。')
    }

    $cnt = Invoke-ExternalCapture -Exe 'docker' -Arguments @('exec', $newContainer, 'psql', '-U', $dbUser, '-d', $dbName, '-tAc', "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema');") -Silent
    if ($null -ne $cnt -and $cnt.Count -gt 0) {
        Write-OkMsg ('库 ' + $dbName + ' 业务表数量: ' + ([string]$cnt[0]).Trim())
    }
}

# ============================================================ [7/7] 切换
Write-StepMsg -Index 7 -Total 7 -Message '切换(默认只打印指令)'

if ($DryRun) {
    Write-DryMsg '演练: 不执行任何切换动作。'
}
elseif (-not $AutoSwitch) {
    Write-WarnMsg '未指定 -AutoSwitch: 新容器 ' + $newContainer + ' 已就绪并通过校验, 但**尚未接管**服务。'
    Write-Host ''
    Write-Host '        确认无误后, 手工完成切换(照抄):' -ForegroundColor Cyan
    Write-Host ''
    Write-Host '          1) 编辑 docker-compose.yml, 把 db 服务的两处改掉:' -ForegroundColor Gray
    Write-Host ('             image: ' + $targetImage) -ForegroundColor Gray
    Write-Host ('             - ' + $newVolume + ':/var/lib/postgresql/data') -ForegroundColor Gray
    Write-Host ('             并在顶层 volumes 段追加 ' + $newVolume + ': external: true') -ForegroundColor Gray
    Write-Host ''
    Write-Host '          2) 停旧容器、起新服务:' -ForegroundColor Gray
    Write-Host ('             docker compose stop db') -ForegroundColor Gray
    Write-Host ('             docker compose up -d db') -ForegroundColor Gray
    Write-Host ''
    Write-Host '          3) 验证业务正常后, 旧容器与旧卷先保留观察:' -ForegroundColor Gray
    Write-Host ('             docker rm ' + $ContainerName + '        # 确认无误后再做') -ForegroundColor Gray
    Write-Host ('             docker volume rm tsp_dbdata           # ⚠️ 最后一步, 会永久删数据') -ForegroundColor Gray
    Write-Host ''
    Write-Host '        想让脚本自动做第 1、2 步, 重跑时加 -AutoSwitch -Yes。' -ForegroundColor Gray
    Write-Host ''
}
else {
    if (-not $Yes) {
        Write-FailMsg '指定了 -AutoSwitch 但没有 -Yes。自动切换会改写 docker-compose.yml 并重启 db 服务, 必须给 -Yes 明示。'
        exit 1
    }

    $composeFile = Join-Path $projectRoot 'docker-compose.yml'
    $backupCopy = Join-Path $env:TEMP ('docker-compose.before-upgrade-' + $stamp + '.yml')
    Copy-Item -LiteralPath $composeFile -Destination $backupCopy -Force
    Write-OkMsg ('已备份 compose 到: ' + $backupCopy)

    $raw = Get-Content -LiteralPath $composeFile -Raw -Encoding UTF8
    $newRaw = $raw -replace 'image:\s*postgres:[0-9][0-9A-Za-z.\-]*', ('image: ' + $targetImage)
    $newRaw = $newRaw -replace 'tsp_dbdata:/var/lib/postgresql/data', ($newVolume + ':/var/lib/postgresql/data')
    if ($newRaw -notmatch ([regex]::Escape($newVolume) + ':')) {
        $newRaw = $newRaw -replace '(volumes:\s*\r?\n\s*tsp_parquet:\s*\r?\n\s*external:\s*true\r?\n)',
        ('$1' + '  tsp_dbdata:' + "`r`n" + '    external: true' + "`r`n" + '  ' + $newVolume + ':' + "`r`n" + '    external: true' + "`r`n")
    }
    [System.IO.File]::WriteAllText($composeFile, $newRaw, (New-Object System.Text.UTF8Encoding($false)))
    Write-OkMsg ('compose 已改写: image -> ' + $targetImage + ', 卷 -> ' + $newVolume)

    & docker compose -f $composeFile stop db
    if ($LASTEXITCODE -ne 0) { Write-WarnMsg '停止旧 db 服务返回非 0, 继续尝试启动新服务。' }
    & docker compose -f $composeFile up -d db
    if ($LASTEXITCODE -ne 0) {
        throw ('切换后启动失败。可回滚: 还原 ' + $backupCopy + ' 到 docker-compose.yml 后 docker compose up -d db。')
    }
    Write-OkMsg '切换完成'
    Write-InfoMsg ('旧容器 ' + $ContainerName + ' 与旧卷 tsp_dbdata 均保留, 确认无误后再手工清理。')
}

Write-SectionMsg '跨大版本升级流程结束'
Write-Host ''
Write-WarnMsg '旧卷 tsp_dbdata 全程未被删除 —— 这是本次升级可回滚的唯一保证, 请在确认新库稳定运行足够久之后再考虑清理。'
Write-Host ''
