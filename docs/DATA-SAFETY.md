# TSP 数据安全红线

> 最后更新：2026-10-08（S2 数据安全基建落地当天；同日晚完成 **DB 实例切换**：
> compose 托管的 `postgres:16.9` / 容器 `tsp_db` 下线，改用外部实例 `postgres-18.6`）
> 适用范围：`E:\ai_codes\ai_personal_panel\tsp-fresh` 及其 Docker 数据卷
>
> 这份文档里的每一条都对应**今天真实发生过的事**，不是通用安全常识的复述。
> 出事的时候照着「第 5 节 恢复演练命令」敲，不要临场发挥。

---

## 0. 一分钟速读（真出事先看这里）

| 你要做的事 | 能不能做 | 做错会怎样 |
|---|---|---|
| `docker compose down` | ✅ 可以 | 容器没了，卷还在 |
| `docker compose down -v` | 🔴 **绝不** | 卷的生命周期保护只有一层，见 §2 |
| `docker volume rm tsp_parquet / tsp_dbdata` | 🔴 **绝不** | 数据当场消失，无回收站（`tsp_dbdata` 已下线，仅作回退保险，删了同样没回收站） |
| `docker volume prune` | 🔴 **绝不** | 同上，而且会顺手删掉别的项目的卷 |
| 停止 / 删除 / 重建 `postgres-18.6` | 🔴 **绝不** | 那是用户自部署的**通用外部库**，不属于 TSP；停机影响的是它上面所有库 |
| `docker compose up -d db` | ❌ 无效 | db 服务已删除，只会报 `no such service`；DB 现在是外部实例，见 §1.1 |
| 升级 Postgres 大版本 | ⚠️ 由实例属主处理 | DB 已迁出 compose，`db-upgrade-major.ps1` 入口会直接退出 2，见 §4 |
| 删镜像 / 删容器瘦身 | ⚠️ 没用 | vhdx **只增不减**，见 §3 |
| 只把备份放 E 盘 | 🔴 不够 | E 盘就是 vhdx 所在的盘，同盘备份等于没备份 |

---

## 1. 数据资产清单：谁是权威副本

### 1.1 权威副本 = Docker named volume（**唯一**）

| 卷名 | 内容 | 2026-10-08 实测 | 容器 |
|---|---|---|---|
| `tsp_parquet` | parquet 行情数据 + 缓存 | **979,180 KiB（≈956 MiB）/ 24,449 个文件** | `TickFlow_Stock_Panel` |
| `tsp_dbdata` | **已下线**（原 postgres:16.9 PGDATA，2026-10-08 随 `tsp_db` 一起下线） | **47,164 KiB（≈46 MiB）** | 原 `tsp_db`（已 stop + rm），保留作回退 |

⚠️ **数据库已经不在本 compose 里了**。2026-10-08 起改用用户自部署的**外部实例**：

| 项 | 值 |
|---|---|
| 容器名 / 镜像 | `postgres-18.6` / `postgres:18.6` |
| 所在网络 | `ying-app-network`（**TSP 不在该网络里**） |
| 宿主端口映射 | `15432` → 容器 `5432` |
| 存储 | bind mount 到 `E:/docker_app_data/postgres_app/data`（容器内 PGDATA `/var/lib/postgresql/18/docker`） |
| TSP 接入路径 | 容器内 `host.docker.internal:15432`（compose 已配 `extra_hosts: host.docker.internal:host-gateway`） |
| TSP 归属 | 库 `tsp` / 用户 `tsp`（owner=tsp），与属主 `ying_admin` 的其它库隔离 |

**为什么走宿主映射而不让 TSP 加入 `ying-app-network`**：改 `networks` 会触发 app 容器
重建，从而中断正在提供服务的 `TickFlow_Stock_Panel`。走 `host.docker.internal:15432`
可以零中断完成切换（实测：容器内 `/dev/tcp/host.docker.internal/15432` = OK，
`127.0.0.1:15432` = FAIL 作为对照组排除假阳性，`tsp` 用户认证连接返回 `PostgreSQL 18.6`）。

卷声明现在只剩 `tsp_parquet`，`tsp_dbdata` 已从 compose 顶层移除（**但卷对象没删**）：

```yaml
volumes:
  tsp_parquet:
    external: true
```

`external: true` 有两个作用，都别去掉：

1. **防项目名前缀**：不加它，compose 会按项目名创建 `tsp-fresh_tsp_parquet`，
   卷名对不上就挂载到一个空卷上，表现是「服务起来了但数据全没了」。
2. **防 `down -v` 误删**：external 卷不由 compose 管理生命周期。

`tsp_dbdata` 虽然已不在 compose 里，但**仍然保留**作为回退保险（重新起一个 16.9
容器就能挂回）。确认 18.6 稳定运行足够久之后再手工 `docker volume rm tsp_dbdata`。

### 1.2 `E:\ai_codes\ai_personal_panel\tsp-fresh\data` **不是备份**

这是今天最容易搞混的一点。S1 迁移（2026-10-08 00:20）把挂载从 bind mount
换成了 `tsp_parquet` 卷之后，`data/` **就不再被任何容器挂载了**。它现在的状态是：

* 目录 mtime 停在 `Oct 8 00:20`（迁移那一刻），**此后不再有任何写入**；
* 实测文件数 24,416，而卷里是 24,449 —— 差的 33 个文件是迁移后美股同步新写的。

也就是说 `data/` 是一张**越放越旧的静止快照**。它挡不住任何事故，最多在
「卷被删了、且事故刚好发生在迁移后 5 分钟内」这种极端情况下救回一点东西。
**不要把它计入备份策略，不要因为「data/ 还在」就放松对卷的保护。**

### 1.3 真正的备份在哪

| 备份 | 路径 | 大小 / 时间 | 同盘？ |
|---|---|---|---|
| parquet 卷 tar | `E:\tsp-backups\tsp_parquet-FROM-SOURCE-20261008-093837.tar` | 748,579,328 B（714 MiB） | E 盘 = vhdx 所在盘 ❌ |
| parquet 卷 tar **异盘副本** | `D:\tsp-backups\tsp_parquet-FROM-SOURCE-20261008-093837.tar` | 748,579,328 B | D 盘（Disk#0）✅ |
| 整盘 vhdx | `E:\DockerBackup\docker_data-20261008-0946.vhdx` | 32,090,619,904 B（29.9 GiB） | E 盘 ❌（仅作整机灾难兜底） |
| Postgres dump | `E:\tsp-backups\db\tsp-db-*.dump` | 846 B（当前空库） | E 盘 ❌ |
| Postgres dump **异盘副本** | `D:\tsp-backups\db\tsp-db-*.dump` | 同上 | D 盘 ✅ |

盘位布局：**D: 在 Disk#0，E: 在 Disk#1（vhdx 也在 E:）**。
所以「备份落在 E 盘」和「数据本身在 E 盘的 vhdx 里」是**同一块物理盘**，
盘挂了两边一起没。异盘副本必须有。

---

## 2. 🔴 红线一：绝不 `docker compose down -v`

### 为什么现在「看起来」安全，但仍然禁止

`external: true` 确实会让 compose 不去删这两个卷。但这条保护**只有一层**：

* 谁哪天为了「让 compose 自己建卷」把 `external: true` 删掉，保护立刻失效；
* `docker volume rm` / `docker volume prune` **完全不受这个约束**，
  prune 尤其危险——它会连带删掉 Dify / RAGFlow 的卷；
* 出事时人是在慌乱状态敲命令的，一层保护不够。

### 正确的下线姿势

用脚本，它会显式拒绝 `-v`：

```powershell
cd E:\ai_codes\ai_personal_panel\tsp-fresh
.\scripts\db-down.ps1
.\scripts\db-down.ps1 -RemoveVolumes   # → [FAIL] 检测到 -RemoveVolumes，exit 1
```

`db-down.ps1` 硬编码拒绝 `-RemoveVolumes`，并在下线前后各打印一次
`tsp_parquet` / `tsp_dbdata` 的存在性，肉眼可核对。

另外它只停 **TSP 自己的服务**：DB 现在是外部实例 `postgres-18.6`，不属于本 compose
项目，脚本会校验待停止列表里没有 `postgres-18.6` 才继续（命中就报错退出 1）。

---

## 3. 🔴 红线二：vhdx 只增不减

今天实测：`docker_data-20261008-0946.vhdx` = **29.9 GiB**，
而里面真正的应用数据只有 `tsp_parquet` 956 MiB（外加已下线的 `tsp_dbdata` 46 MiB，
现已不再被任何容器挂载，只是留着没删）。剩下的全是历史层——**包括已经删掉的镜像、
已经停掉的容器写过的文件**。

> 现在的库 `postgres-18.6` 用的是 bind mount（`E:/docker_app_data/postgres_app/data`），
> 数据在宿主 NTFS 上，**不计入 vhdx**。所以别再拿 vhdx 大小去估 DB 体量。

WSL2 的 ext4.vhdx **不会自动回收空间**。所以：

* 删镜像、删容器、`docker system prune` **都不减小 vhdx 文件**；
* 往容器可写层里拷大文件（比如把 dump 拷进去做恢复）会**永久**占位，
  所以恢复完一定记得 `docker exec <容器> rm -f /tmp/restore.dump`；
* 想真瘦身必须：关 Docker Desktop → `wsl --shutdown` → `optimize-vhd`
  （这一步会让 Docker 整体停机，**别在服务跑着的时候做**）。

```powershell
# 瘦身（会停掉所有容器，含 Dify / RAGFlow，挑没人用的时候做）
wsl --shutdown
optimize-vhd -Path "<Docker Desktop 实际使用的 ext4.vhdx 路径>" -Mode full
```

> vhdx 路径以 Docker Desktop → Settings → Resources → Disk image location 为准，
> 不要凭记忆填。今天备份出来的那份在 `E:\DockerBackup\docker_data-20261008-0946.vhdx`。

---

## 4. 🔴 红线三：Postgres 跨大版本只能逻辑迁移 —— 但**当前实例不由我们升级**

PGDATA 格式**跨大版本不兼容**。把 `postgres:16.9` 换成 `postgres:17`
直接指到同一个 `tsp_dbdata`，结果只有一个：容器起不来，日志里一句
`database files are incompatible with server`。唯一安全路径始终是逻辑迁移：
**`pg_dumpall` → 新建空卷 + 新版本容器 → 导入 → 校验 → 切换**。

### 4.1 但这条红线现在**不适用**于 TSP 自己的脚本

因为 DB 已经不是我们的了。2026-10-08 起 TSP 用的是用户自部署的**外部实例**
`postgres-18.6`（网络 `ying-app-network`，宿主端口 `15432`）：

* 它不在 `docker-compose.yml` 里，`docker compose down/up` 碰不到它；
* 它上面跑着属主 `ying_admin` 的其它库，**不属于 TSP**；
* 它的 PGDATA 在宿主的 bind mount 上，不在 `tsp_dbdata` 卷里。

所以：**Postgres 大版本升级请由该实例的属主自行处理**。TSP 侧的任何脚本都不得
停止 / 重建 / 升级 `postgres-18.6`。

`db-upgrade-major.ps1` 已经做了 fail-closed 处理：入口先检测 `docker-compose.yml`
里是否仍有 `db:` 服务定义，没有就打印明确错误并以**退出码 2**退出：

```powershell
.\scripts\db-upgrade-major.ps1 -TargetVersion 19
# → [FAIL] docker-compose.yml 里已没有 db 服务 —— 本脚本的前提不成立, 已中止。
# → 退出码 2（实测）
```

脚本主体代码**完整保留**（7 步逻辑迁移流程没删），将来若 DB 重新收回 compose
托管，恢复 `db` 服务定义后可原样复用。

### 4.2 将来若要给外部实例做大版本升级（属主操作参考）

1. 先 `pg_dumpall` 全量逻辑导出（含 role 和全部 database）；
2. 用新版本镜像起**全新的空数据目录**（不是复用旧 PGDATA）；
3. 逻辑导入 → 校验版本 / database 列表 / 业务表数量；
4. 校验通过后再切换端口或连接串。

⚠️ 即使是将来，也不要在 TSP 仓库里用 `db-upgrade-major.ps1` 对外部实例执行这些
动作 —— 那个脚本只会碰 compose 里的容器。

**`tsp_dbdata`（旧卷）脚本永远不会删**，切换后要人工确认无误再自行处理。

---

## 5. 恢复演练命令（真出事时照抄）

### 5.0 前置：Git Bash 的路径转换陷阱

**所有带绝对路径参数的 `docker run` / `docker exec`，在 Git Bash 下都必须加
`MSYS_NO_PATHCONV=1`**，否则 `/app/...`、`/dest` 这类路径会被改写成
`C:/Program Files/Git/...`，命令以莫名其妙的方式失败。今天已经在
`docker exec /app/.venv/bin/python` 和 `tar -C /dest` 上各踩过一次。
（详情见 `scripts/README-部署.md` FAQ #9。）

### 5.1 Postgres 恢复（custom 格式 dump）

> 目标容器是**外部实例 `postgres-18.6`**（网络 `ying-app-network`，宿主端口 `15432`，
> 镜像 `postgres:18.6`）。它不在 TSP 的 compose 项目里，所以只能按**容器名**操作
> （`docker cp` / `docker exec`），不能用 `docker compose ... db`。
> 如果哪天需要走网络而不是容器名，主机/端口就是 `.env` 里的
> `POSTGRES_HOST=host.docker.internal` + `POSTGRES_PORT=15432`（**不是** 5432）。

> ⚠️ **`pg_restore` 不接受 `-` 表示 stdin**（16 和 18 都一样）。今天 21:39 首次真实
> 演练就是挂在 `docker exec -i postgres-18.6 pg_restore -l - < file` 上，报错
> `could not open input file "-": No such file or directory`，退出码 1。
> 必须让 pg_restore 读**真实文件路径**。

```bash
# 1) 把归档放进容器
MSYS_NO_PATHCONV=1 docker cp "E:\tsp-backups\db\tsp-db-tsp-20261008-214159.dump" postgres-18.6:/tmp/restore.dump

# 2) 恢复（--clean --if-exists 会先删同名对象，可重复执行）
MSYS_NO_PATHCONV=1 docker exec postgres-18.6 pg_restore -U tsp -d tsp --clean --if-exists /tmp/restore.dump

# 3) 清理容器内临时归档
MSYS_NO_PATHCONV=1 docker exec postgres-18.6 rm -f /tmp/restore.dump
```

> 第 3 步现在主要是卫生习惯而不是 vhdx 问题：18.6 的数据目录是宿主 bind mount
> （`E:/docker_app_data/postgres_app/data`），`/tmp` 仍在容器可写层里，还是会占
> vhdx（见 §3），所以照样别留。

恢复前先确认归档没坏：

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "E:\tsp-backups\db:/tsp_dump_check:ro" \
  --entrypoint pg_restore postgres:18.6 -l /tsp_dump_check/tsp-db-tsp-20261008-214159.dump
```

正常输出应包含 `; Archive created at ...`、`; TOC Entries: 4`、`; Format: CUSTOM`；
18.6 实例导出时还会带
`; Dumped from database version: 18.6 (Debian 18.6-1.pgdg13+2)`。
**用 `docker run` 起一次性容器来校验，不要用 `docker exec`**（同版本 pg_restore
读宿主机文件最干净，且不留任何东西在 vhdx 里）。

### 5.2 parquet 卷恢复（从 tar）

```bash
# 0) 先看清 tar 内部结构，再决定要不要 / 怎么加 -C
tar -tf /d/tsp-backups/tsp_parquet-FROM-SOURCE-20261008-093837.tar | head

# 1) 卷没了就重建（还在就跳过）
docker volume create tsp_parquet

# 2) 解回卷里
MSYS_NO_PATHCONV=1 docker run --rm \
  -v tsp_parquet:/dest \
  -v "D:\tsp-backups:/src:ro" \
  alpine:latest sh -c "cd /dest && tar -xf /src/tsp_parquet-FROM-SOURCE-20261008-093837.tar"

# 3) 校验：文件数应对得上（今天基准 24,449，迁移当时 24,414）
MSYS_NO_PATHCONV=1 docker run --rm -v tsp_parquet:/p alpine:latest sh -c "find /p -type f | wc -l"
```

### 5.3 parquet 卷恢复（卷没了、tar 也没了，只有 `data/`）

**这是兜底中的兜底**，只能拿回迁移时刻的旧快照（见 §1.2）：

```bash
docker volume create tsp_parquet
MSYS_NO_PATHCONV=1 docker run --rm \
  -v tsp_parquet:/dest \
  -v "E:\ai_codes\ai_personal_panel\tsp-fresh\data:/src:ro" \
  alpine:latest sh -c "cp -a /src/. /dest/"
```

> 注意：`data/` 是 NTFS bind 挂载。polars 的 mmap 在 NTFS bind mount 上会失败，
> 所以**拷进卷之后**再从卷里跑服务，不要图省事直接挂 `data/` 跑——
> 这正是当初 S1 要迁到 named volume 的原因之一。

### 5.4 整机灾难（vhdx 级别）

1. 关 Docker Desktop；
2. `wsl --shutdown`；
3. 用 `E:\DockerBackup\docker_data-20261008-0946.vhdx` 替换 Docker Desktop
   实际使用的 ext4.vhdx（**先给当前 vhdx 改名留一份，别覆盖**）；
4. 启动 Docker Desktop，`docker volume ls` 确认 `tsp_parquet` 在（`tsp_dbdata`
   也应在，它现在只是回退保险）；
5. `postgres-18.6` 的数据在宿主 `E:/docker_app_data/postgres_app/data` 上，
   **不在 vhdx 里**，所以整机灾难时要单独确认这份 bind mount 的备份情况。

---

## 6. 日常备份：一条命令

```powershell
cd E:\ai_codes\ai_personal_panel\tsp-fresh
.\scripts\db-backup.ps1              # 默认容器 postgres-18.6，保留 14 份，自动出 D 盘副本
.\scripts\db-backup.ps1 -DryRun      # 演练，不产生任何文件
.\scripts\db-backup.ps1 -KeepBackups 30
```

默认目标已经是外部实例 `postgres-18.6`（不再是 `tsp_db`）。宿主端口从 `.env` 的
`POSTGRES_PORT` 读，缺失时回落 `15432`（不硬编码 5432）。脚本只做导出，不会停止或
重启该容器。

> 外部实例**没有配 healthcheck**，所以 `docker inspect ... {{.State.Health.Status}}`
> 会报 `map has no entry for key "Health"`。脚本因此只看 `State.Status`，真正的存活
> 性由 `docker exec <容器> pg_isready` 把关 —— 比 healthcheck 更直接。

六步，每步都会失败即停（`throw`）：

| 步 | 做什么 | 失败会怎样 |
|---|---|---|
| [1/6] | 预检 docker / 容器 running + `pg_isready` 通过 / .env 里 `POSTGRES_*` | 缺变量或连不上直接停 |
| [2/6] | 生成 `tsp-db-<db>-<yyyyMMdd-HHmmss>.dump` | — |
| [3/6] | `pg_dump -Fc`，重定向交给 `cmd /c` | 非 0 退出码即停 |
| [4/6] | 非空校验 + `pg_restore -l` 结构校验 | 坏档立刻报 |
| [5/6] | 复制到 D 盘并**比对字节数** | 大小不一致即停 |
| [6/6] | 按保留策略清理两个目录 | — |

### 两个已经踩过、脚本里已规避的坑

1. **PowerShell 的 `>` 会重编码字节流**，直接用来接 `pg_dump -Fc` 的二进制输出
   = 毁档。脚本里所有 dump 重定向都走 `cmd /c` 原生重定向。
2. **`pg_restore` 不吃 stdin**（§5.1）。校验改成 `docker run --rm -v` 挂载读文件。

---

## 7. 密码与凭据

* Postgres 密码是 40 位随机串，只存在于 `E:\ai_codes\ai_personal_panel\tsp-fresh\.env`；
* `.env` 被 `.gitignore:55:.env` 忽略，**不进版本控制**；
* 现在 `docker-compose.yml` **不再引用任何 `POSTGRES_*` 变量**（db 服务已移除），
  凭据只由 `.env` 提供给将来接线的应用代码；任何脚本/文档都不得硬编码密码；
* 备份脚本走 `docker exec` + 容器内 Unix socket，**密码不出现在命令行里**，
  也就不会出现在 shell history / 进程列表 / 日志里。若将来改成经 `15432` 走网络，
  请务必用 `PGPASSWORD` 环境变量或 `~/.pgpass` 传递，**不要**写成 `-p <密码>`；
* 提交前务必 `git status --short` 确认没有密码文件被 stage。

```bash
git check-ignore -v .env      # 应输出 .gitignore:55:.env
git status --short            # 不应出现 .env
```

---

## 8. 例行检查清单

**每次动 compose / 卷之后：**

- [ ] `docker volume ls | grep tsp` —— `tsp_parquet` 在，`tsp_dbdata` 也应在
      （已下线，仅作回退保险，别删）；
- [ ] `docker ps` —— `TickFlow_Stock_Panel` 和 **`postgres-18.6`** 都 Up。
      注意 `postgres-18.6` 是**外部实例**：它在网络 `ying-app-network` 上，
      **不在** TSP 的 compose 项目里；TSP 是经宿主端口 `15432`
      （容器内 `host.docker.internal:15432`）访问它的。所以 `docker compose ps`
      里**看不到**它是正常的，要用 `docker ps` 看；
- [ ] `docker inspect -f '{{.State.StartedAt}}' TickFlow_Stock_Panel`
      —— 和动之前一致（今天基准 `2026-10-08T02:57:55.86037531Z`），
      **变了就说明 TSP 被重启过**。

**每周：**

- [ ] `.\scripts\db-backup.ps1` 跑一次，确认 D 盘副本字节数一致；
- [ ] 看一眼 E 盘剩余空间（vhdx 只增不减，会吃掉它）。

**每次要大改之前：**

- [ ] 先按 §5 里对应的恢复命令**真的恢复一次**到临时目标，确认备份可用。
      没演练过的备份不算备份。

---

## 9. 相关脚本

| 脚本 | 用途 |
|---|---|
| `scripts/db-backup.ps1` | 外部实例 `postgres-18.6` 备份 + 异盘副本 + 保留策略 + 可恢复性校验 |
| `scripts/db-down.ps1` | 下线 TSP 服务（**不带 `-v`**，显式拒绝 `-RemoveVolumes`，且不碰外部 DB 实例） |
| `scripts/db-upgrade-major.ps1` | Postgres 跨大版本逻辑迁移 —— **当前前提不成立，入口 fail-closed 退出 2** |
| `scripts/deploy.ps1` / `rollback.ps1` | TSP 应用发布 / 回滚 |
| `scripts/export-parquet.ps1` | parquet 导出 |
| `scripts/deploy-common.ps1` | 公共函数库（输出、命令执行、compose 调用） |
