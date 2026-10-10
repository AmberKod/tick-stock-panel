# tsp-fresh 一键部署脚本（PowerShell）

两个脚本把「宿主机改代码 → 打包镜像 → 部署 → 健康检查 → 失败自动回滚」串成一条命令。

| 文件 | 作用 |
| --- | --- |
| `update-service.bat`（项目根） | **双击即可更新**：改代码 → 重新打包镜像 → 重建容器 → 健康检查（失败自动回滚）。执行策略/编码都已处理，无障碍入口 |
| `start-host-deps.bat`（项目根） | 双击启动两个宿主常驻依赖（sec_relay / OpenBB），机器重启后用它恢复美股深度数据面板 |
| `scripts/deploy.ps1` | 主流程：检查 → 生成 tag → 构建 → 部署 → 健康检查（失败自动回滚）→ 清理旧镜像 |
| `scripts/rollback.ps1` | 单独回滚到上一版或指定 tag |
| `scripts/deploy-common.ps1` | 两个脚本共用的函数库（日志、命令执行、Docker 检测、tag 状态文件、健康检查等），**不要单独执行** |

脚本**不会**修改 `Dockerfile`、`docker-compose.yml`、`.dockerignore`，也**不会**碰 `data/` 目录。

> **为什么额外提供 `.bat`？**
> PowerShell 的执行策略默认会拦住 `.ps1`（双击常常变成"用记事本打开"或直接被拒绝），
> 而 Windows 控制台默认 GBK 代码页会把批处理里的中文读成乱码。
> 所以 `.bat` 只做三件事：**绕开执行策略**、**保持 ASCII 内容**（中文输出全交给 UTF-8-BOM 的 PowerShell）、**跑完暂停让你看清结果**。

---

## 只想双击更新的人（从这里开始）

直接双击项目根目录的 **`update-service.bat`**。它会依次完成：

| 步骤 | 内容 |
| --- | --- |
| 1/6 | 前置检查：Docker/Compose 可用性、`.env` 是否存在、是否有后台任务正在写数据 |
| 2/6 | 生成镜像 tag 并记录（同时把当前运行中的镜像锚定为回滚点） |
| 3/6 | `docker compose build` 重新打包镜像 |
| 4/6 | `docker compose up -d --force-recreate` 重建容器上线 |
| 5/6 | 轮询 `http://127.0.0.1:<port>/health`，**失败自动回滚到上一版镜像** |
| 6/6 | 清理旧镜像（默认保留最近 5 个 tag） |

跑完会打印前端访问地址，以及 sec_relay / OpenBB 两个**宿主常驻依赖**的状态。

> **「正在写数据」保护是两级判定**（2026-10-09 修正，此前是错误设计）：
>
> - **服务在跑**（端口监听、容器 running）→ 只是**现状信息**，绿色打印，**不拦截**。因为更新已部署的服务时它本来就在跑，若拿这条拦截会导致每次双击都中止。
> - **真写入证据** → 才中止：① 后端 `/api/pipeline/jobs` 自报有活动任务（`active_id` 非空，或存在 `running/pending/queued`）；② `data/` 下的 `*.log` / `*.lock` / 一级子目录在 `-BusyWindowSec`（默认 60 秒）内被更新。
>
> 只有被真证据拦住时才需要加 `-Force`：
>
> ```powershell
> powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -Force
> ```

---

## 前置条件

1. **Docker Desktop 已启动**（`docker version` 能返回 Server 版本）。脚本第一步就会检查，起不来会直接报中文错误。
2. **项目根存在 `.env` 文件**。
   `docker-compose.yml` 里用了 `env_file: [.env]` 并且把 `./.env` 只读挂载进容器，所以 **`.env` 缺失会让 `docker compose` 直接报错退出**，这是硬性前置检查。
   首次部署请先执行：

   ```powershell
   Copy-Item .env.example .env
   # 然后按需填写 TICKFLOW_API_KEY / AI_API_KEY / AUTH_PASSWORD
   ```

   `.env` 已在 `.gitignore` 中，不会被提交。
3. **PowerShell 5.1 或更高版本**（Windows 11 自带的是 5.1，可直接使用）。
4. 在**项目根目录**或任意目录执行均可——脚本用自身位置推导项目根，不依赖当前工作目录。

---

## 用法

### 先演练（推荐第一次都这么做）

```powershell
.\scripts\deploy.ps1 -DryRun
```

只打印将要执行的命令序列，**不构建、不启动容器、不写任何文件**。

### 标准一键部署

```powershell
.\scripts\deploy.ps1
```

流程输出类似：

```
[1/6] 检查 Docker / Compose / .env / 数据写入状态
  [OK] Docker 可用, Server 版本: 29.8.2
  [OK] 找到 .env 文件(Compose env_file 与只读挂载均需要它)
       健康检查地址: http://127.0.0.1:3018/health
  [OK] 未检测到正在写数据的迹象。
       git 分支: develop , HEAD: e39c628 , 未提交条目: 3 个
[2/6] 生成本次部署的镜像 tag 并记录状态
       镜像名      : tsp-fresh-app
       本次 tag    : e39c628-dirty-20261007-234500
[3/6] docker compose build
       $ docker compose build
       $ docker tag tsp-fresh-app:latest tsp-fresh-app:e39c628-dirty-20261007-234500
[4/6] docker compose up -d
       $ docker compose up -d
[5/6] 轮询 http://127.0.0.1:3018/health (最多 90 秒)
  [OK] 部署成功, 服务已通过健康检查。
[6/6] 保留最近 5 个 tsp-fresh-app 的 tag
```

### 常用参数

| 参数 | 说明 |
| --- | --- |
| `-DryRun` | 演练，只打印命令，零副作用 |
| `-SkipBuild` | 跳过构建，只用现有镜像重新部署（改了 `.env` 这类运行时配置时用） |
| `-SkipRecreate` | 不强制重建容器。**默认行为是强制重建**，见下方说明，一般不用动 |

> **`up -d` 为什么默认带 `--force-recreate`（勿随手删）**
> compose 的 `env_file:` 会把 `.env` 的值在**容器创建时**固化进容器内的 `os.environ`，
> 而 pydantic-settings 的优先级是 **环境变量 > `.env` 文件**。于是当镜像内容没变时
> （典型场景：`-SkipBuild` 只改了 `.env`）compose 不会重建容器，**旧环境变量会遮蔽新的 `.env`**，
> 表现为「改了配置却没生效」。`docker restart` 同样无效——必须重建容器。
| `-Force` | 忽略「检测到正在写数据」的警告，强制部署 |
| `-NoRollback` | 健康检查失败时不自动回滚，保留现场排查 |
| `-RunTests` | 部署前跑一次后端测试子集（默认不跑，全量测试耗时很长） |
| `-TestFilter 'not slow'` | 配合 `-RunTests` 的 pytest `-k` 过滤表达式 |
| `-Port 3018` | 手动指定健康检查端口（默认读 `.env` 的 `PORT`，读不到用 3018） |
| `-HealthTimeoutSec 90` | 健康检查总超时 |
| `-BusyWindowSec 60` | 「正在写数据」判定窗口（秒） |
| `-KeepImages 5` | 清理时保留最近几个镜像 tag |

### 回滚

```powershell
.\scripts\rollback.ps1              # 回滚到 .prev-deploy-tag 记录的上一版
.\scripts\rollback.ps1 -Tag e39c628 # 回滚到指定 tag
.\scripts\rollback.ps1 -DryRun      # 演练回滚
```

回滚成功后会轮换状态文件：`.last-deploy-tag` 写入**回滚后的**版本（当前真正跑着的），`.prev-deploy-tag` 写入**回滚前的**版本，所以再执行一次 `rollback.ps1` 可以「向前滚回」。

---

## 镜像 tag 与状态文件

- 镜像名由 `docker compose config --images` 动态解析（本项目是 `tsp-fresh-app`），**脚本里没有硬编码**。
  之所以要动态解析：compose 文件里没有 `image:` 键，构建产物固定是 `<项目名>-<service>:latest`。
- 构建完成后脚本用 `docker tag` 补打本次版本 tag，同时保留 `:latest`（compose 仍然引用 `:latest`）。
- tag 取值规则：
  - 工作区干净 → `git rev-parse --short HEAD`（如 `e39c628`）
  - 工作区有未提交改动 → `<hash>-dirty-<yyyyMMdd-HHmmss>`（避免两次构建复用同一 tag，导致回滚指向同一份镜像）
  - 不是 git 仓库 / 取不到 hash → `yyyyMMdd-HHmmss`
- 状态文件（建议加到 `.gitignore`，它们只是本机部署状态）：

  ```
  scripts/.last-deploy-tag   # 当前部署的 tag
  scripts/.prev-deploy-tag   # 上一版 tag（回滚目标）
  ```

- 清理策略：只处理 Repository 等于本项目镜像名的行，**不会误删其他项目的镜像**；`latest` 与 `<none>` 不纳入统计且 `latest` 永不删除；当前版本与上一版本受保护；其余保留最近 5 个，删除前会先打印清单。

---

## 常见问题

### 1. `[FAIL] docker 守护进程不可用`

Docker Desktop 没启动或还在初始化。打开 Docker Desktop 等状态变成 Running 后重试。

### 2. `[FAIL] 前置检查未通过: 缺少 .env 文件`

见上面的前置条件 2，执行 `Copy-Item .env.example .env` 后再部署。

### 3. 部署被「正在写数据」警告拦住

输出会是醒目黄框，框里只列**真写入证据**，例如：

```
       现状: 端口 3018 正在监听(服务在运行, 属正常现象)
       现状: Compose 容器 TickFlow_Stock_Panel 处于 running 状态
************************************************************
*  警告: 检测到后端可能正在写数据 / 正在运行              *
************************************************************
  * - 后台任务 0599a045c9 状态=running 阶段=market_daily_sync 进度=42%
  * - data 目录最近 12 秒内有写入(最新: backend.log)
************************************************************
[FAIL] 已中止部署。确认安全后请加 -Force 参数重新执行。
```

**两级判定**（2026-10-09 修正）：

- **不拦截**（现状信息，绿色）：① 服务端口在监听；② 有 `running` 容器。头两条只是告诉你"服务本来就在跑"。
- **会拦截**（写入证据）：① `/api/pipeline/jobs` 报告有活动任务；② `data/` 下的 `*.log`、`*.lock` 或一级子目录在最近 60 秒（`-BusyWindowSec`）内被更新。

> 2026-10-09 之前这里是**错误设计**：把"端口在监听"和"容器 running"也算成写入信号，而更新一个已部署的服务时这两条恒为真 ⇒ **每次双击都会被拦**。现已按上面的两级重构。

**为什么默认拦**：项目里跑过长任务（例如 `market_daily_sync` 实测跑了 98 分钟），在服务写数据中途重启容器可能中断任务或留下半截文件。确认任务结束后重试即可；确实要强推就加 `-Force`。

> `-DryRun` 时这一步**不会**真的中止——会打印一句「真实执行时会在这一步中止」然后继续，方便你看到完整流程。

> 补充：镜像本身不含 `data`（`.dockerignore` 已排除，数据是运行时 bind mount 进去的），所以「正在写数据」的风险不在构建阶段，而在**重启容器**这一下。

### 4. 健康检查超时

- 端点确认是 **`http://127.0.0.1:3018/health`**（不是 `/api/health`）。
- 端口来自 `.env` 的 `PORT`；如果 `.env` 里改过端口，脚本会自动跟随，也可用 `-Port` 手动指定。
- 90 秒不够（首次启动要初始化）可以加 `-HealthTimeoutSec 180`。
- 排查：`docker compose logs --tail 200`、`docker compose ps`。
- 健康检查失败默认会**自动回滚**到上一版；想保留现场就加 `-NoRollback`。

### 5. 端口被占用

compose 映射的是 `${HOST:-0.0.0.0}:${PORT:-3018}:3018`。如果 3018 已被别的进程占了，`docker compose up -d` 会报端口冲突。
先查是谁占用：

```powershell
Get-NetTCPConnection -LocalPort 3018 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Get-Process -Id $_ }
```

要么停掉占用进程，要么改 `.env` 里的 `PORT` 后重新部署。

### 6. `docker compose build` 秒失败：`failed to get console: The handle is invalid.`

典型输出（构建压根没开始，不到一秒就退）：

```
$ docker compose build
WARN[0000] The "HOME" variable is not set. Defaulting to a blank string.
failed to get console: The handle is invalid.
[FAIL] 构建失败: 命令执行失败(退出码 1) : docker compose build
```

**根因**：双击 `.bat` 的进程链是 `Explorer → conhost → cmd → powershell → docker compose`。在这条链上 compose 会把自己的 stdout 判定为终端（证据：它的 logrus 输出切成了带 `[NNNN]` 偏移的着色格式 `WARN[0000] …`），随即去取控制台句柄，而该句柄在此上下文里无效；同一个命令在常规终端里是好的，所以极难自查。

**已修（2026-10-10）**，两道保险，都在 `deploy-common.ps1` 里：

1. `Invoke-ExternalCommand` 改用 .NET `Process` 启动子进程，**三条标准流全部重定向**（stdin 立刻 EOF、stdout/stderr 走管道）→ 子进程 `isatty()` 全为 false，compose 不再碰控制台。stderr 异步抽干，防止管道缓冲区写满死锁。
2. `Invoke-ComposeCommand` 给所有 compose 子命令加全局开关 `--progress plain --ansi never`（`docker compose --progress plain --ansi never build`）。

顺带：`deploy.ps1` 与 `update-service.bat` 都补了 `HOME` 兜底（双击时 Windows 只给 `USERPROFILE`），消除那条 WARN。

### 7. 回滚失败：目标镜像不存在

镜像可能已被清理（只保留最近 5 个 tag）或从未构建过。先列出可用 tag：

```powershell
docker images tsp-fresh-app --format "{{.Repository}}:{{.Tag}}"
```

然后用 `-Tag` 指定一个存在的版本。

### 7. PowerShell 执行策略不允许运行脚本

```
无法加载文件 ... 因为在此系统上禁止运行脚本。
```

用下面任一方式解决（推荐只对当前用户放开）：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# 或者只在当前进程临时放开，不影响系统设置
powershell -ExecutionPolicy Bypass -File .\scripts\deploy.ps1 -DryRun
```

### 8. 中文输出乱码

本套脚本必须以 **UTF-8 with BOM** 保存。Windows PowerShell 5.1 在没有 BOM 时会按系统 ANSI 代码页解析 `.ps1`，中文字符串会变成乱码。若你用编辑器改动过这些脚本，请确认保存编码仍是 UTF-8 with BOM。

### 9. Git Bash 里 `docker exec ... -C /dest` 报 `can't change directory`

在 **Git Bash（MSYS2）** 下把绝对路径传给容器内的命令时，MSYS 会自作主张做路径转换。例如：

```bash
docker exec -i <容器> tar -xf - -C /dest
# 实际被展开成: -C C:/Program Files/WorkBuddy/resources/vendor/PortableGit/dest
# 报错: tar: can't change directory to 'C:/Program Files/.../PortableGit/dest'
```

这是因为 MSYS 认为 `/dest` 是 POSIX 根路径，把它映射到了 Git 的安装目录。**解决方法**：在命令前加 `MSYS_NO_PATHCONV=1`。

```bash
export MSYS_NO_PATHCONV=1
tar -cf - -C data --exclude='backend.log*' . | docker exec -i <容器> tar -xf - -C /dest
```

> 本套 PowerShell 脚本不受影响（PowerShell 不做这种转换）。只有当你用 Git Bash 手工执行容器命令时才需要注意。
>
> **规则要放大一点记**：Git Bash 下**凡是 `docker exec` / `docker run` 的命令行里出现容器内的绝对路径参数**，都要加 `MSYS_NO_PATHCONV=1`，不限于 `tar -C`。实测踩过两次：
>
> ```bash
> # 1) tar -C 被转换（S1 迁移时）
> docker exec -i <容器> tar -xf - -C /dest
>
> # 2) 容器内可执行文件路径也被转换（启动后排查依赖时）
> docker exec TickFlow_Stock_Panel /app/.venv/bin/python -c "import requests"
> # 报错: exec: "C:/Program Files/WorkBuddy/resources/vendor/PortableGit/app/.venv/bin/python":
> #       stat ...: no such file or directory
> ```
>
> 第二种尤其容易被误判成"镜像里没有这个路径"，实际是宿主机 shell 把参数改写了。稳妥做法是在排查会话里一次性导出：
>
> ```bash
> export MSYS_NO_PATHCONV=1
> ```

---

## 附：S1 数据迁移记录（`tsp_parquet`）

把 `data/` 复制进 named volume 的实测记录（2026-10-08），供后续阶段参考，避免重复试错。

**结论**：volume 名 **`tsp_parquet`**，迁移耗时 **59 秒**。

**命令**（在 Git Bash 下，注意 `MSYS_NO_PATHCONV=1`）：

```bash
docker volume create tsp_parquet
docker run -d --name tsp_migrate_tmp -v tsp_parquet:/dest alpine:latest sleep 3600

export MSYS_NO_PATHCONV=1
tar -cf - -C data --exclude='backend.log*' . | \
  docker exec -i tsp_migrate_tmp tar -xf - -C /dest

docker rm -f tsp_migrate_tmp     # 只删容器, volume 保留
```

**为何用 tar 流式、不用容器内 `cp -a`**：容器内 `cp -a` 走 bind mount 读取源目录时，只要后端还在写 `data/backend.log` 就会 `cp: read error: I/O error`。宿主机 tar 同样受影响，所以**迁移前必须确认后端已停**、`data/` 近 60 秒无任何文件 mtime 更新。

**实测结果**：

| 项 | 值 |
| --- | --- |
| 迁移耗时 | 59 秒 |
| 卷内文件数 | 24,414（与源逐个对齐） |
| 卷内 parquet | 21,426 |
| 卷内目录数 | 21,533 |
| `backend.log*` | 0（`--exclude` 生效） |
| 卷内体积 | 831,660 KiB = **812.3 MiB** |

**关于体积膨胀 +18.7%（不是 +12%）**：源 700,696 KiB → 卷 831,660 KiB。差异可完整对账为两部分：24,414 个文件各自向上取整到 ext4 4K 块（约 +49 MB），**外加 21,533 个目录各占一个 4K 块（约 +84 MB）**。常见估算里的 "+12%" 只算了文件块对齐、漏了目录块——这个项目目录数几乎和文件数一样多，所以目录开销异常大。**这是正常的块对齐膨胀，不是错误。**

**Polars 校验**：在容器里读卷内 parquet，`kline_daily_enriched` = **1,320,178 行**（迁移前架构师给的基线值，精确命中）。

**原目录必须原样保留**：这是复制语义，`data/` 为未来 30 天的回滚保障。迁移后应抽查若干文件确认 size / mtime 未变。

---

## 实现细节备注（给维护者）

- 所有外部命令都走 `Invoke-ExternalCommand`，统一打印命令行并检查 `$LASTEXITCODE`，非预期退出码抛中文错误；只读查询走 `Invoke-ExternalCapture`，失败返回 `$null` 由调用方决定降级策略。
- 全程不使用 PowerShell 7+ 专有语法（`&&`、`||`、`??`、三元 `?:`），5.1 / 7+ 均可运行。
- `.dockerignore` 已排除 `scripts`，本目录不会进入构建上下文。
