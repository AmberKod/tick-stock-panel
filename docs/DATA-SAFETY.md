# TSP 数据安全红线

> 最后更新：2026-10-08（S2 数据安全基建落地当天）
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
| `docker volume rm tsp_parquet / tsp_dbdata` | 🔴 **绝不** | 数据当场消失，无回收站 |
| `docker volume prune` | 🔴 **绝不** | 同上，而且会顺手删掉别的项目的卷 |
| 升级 Postgres 大版本 | ⚠️ 只能走 §4 | 直接换镜像 = PGDATA  incompatible，起不来 |
| 删镜像 / 删容器瘦身 | ⚠️ 没用 | vhdx **只增不减**，见 §3 |
| 只把备份放 E 盘 | 🔴 不够 | E 盘就是 vhdx 所在的盘，同盘备份等于没备份 |

---

## 1. 数据资产清单：谁是权威副本

### 1.1 权威副本 = Docker named volume（**唯一**）

| 卷名 | 内容 | 2026-10-08 实测 | 容器 |
|---|---|---|---|
| `tsp_parquet` | parquet 行情数据 + 缓存 | **979,180 KiB（≈956 MiB）/ 24,449 个文件** | `TickFlow_Stock_Panel` |
| `tsp_dbdata` | Postgres 16.9 PGDATA | **47,164 KiB（≈46 MiB）** | `tsp_db` |

两个卷都在 `docker-compose.yml` 顶层声明为 `external: true`：

```yaml
volumes:
  tsp_parquet:
    external: true
  tsp_dbdata:
    external: true
```

`external: true` 有两个作用，都别去掉：

1. **防项目名前缀**：不加它，compose 会按项目名创建 `tsp-fresh_tsp_parquet`，
   卷名对不上就挂载到一个空卷上，表现是「服务起来了但数据全没了」。
2. **防 `down -v` 误删**：external 卷不由 compose 管理生命周期。

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

---

## 3. 🔴 红线二：vhdx 只增不减

今天实测：`docker_data-20261008-0946.vhdx` = **29.9 GiB**，
而里面真正的应用数据只有 `tsp_parquet` 956 MiB + `tsp_dbdata` 46 MiB。
剩下的全是历史层——**包括已经删掉的镜像、已经停掉的容器写过的文件**。

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

## 4. 🔴 红线三：Postgres 跨大版本只能逻辑迁移

PGDATA 格式**跨大版本不兼容**。把 `postgres:16.9` 换成 `postgres:17`
直接指到同一个 `tsp_dbdata`，结果只有一个：容器起不来，日志里一句
`database files are incompatible with server`。

唯一安全路径：**`pg_dumpall` → 新建空卷 + 新版本容器 → 导入 → 校验 → 切换**。
已封装成脚本，强制先备份：

```powershell
.\scripts\db-upgrade-major.ps1 -TargetVersion 17
```

脚本行为（7 步）：

1. 预检 docker / 容器 / .env，读出当前主版本（今天实测 16）；
2. 拒绝降级和同大版本；
3. **强制先跑一次 `db-backup.ps1`**（除非显式 `-SkipBackup -Yes`，不推荐）；
4. `pg_dumpall` 全量逻辑导出（含 role 和全部 database）；
5. 新建卷 `tsp_dbdata_v17`，起新容器 `tsp_db_v17`；
6. `psql` 导入 + 校验；
7. 切换：默认只打印手工切换步骤，`-AutoSwitch -Yes` 才自动切。

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

> ⚠️ **PG 16 的 `pg_restore` 不接受 `-` 表示 stdin**。今天 21:39 首次真实演练
> 就是挂在 `docker exec -i tsp_db pg_restore -l - < file` 上，报错
> `could not open input file "-": No such file or directory`，退出码 1。
> 必须让 pg_restore 读**真实文件路径**。

```bash
# 1) 把归档放进容器
MSYS_NO_PATHCONV=1 docker cp "E:\tsp-backups\db\tsp-db-tsp-20261008-214159.dump" tsp_db:/tmp/restore.dump

# 2) 恢复（--clean --if-exists 会先删同名对象，可重复执行）
MSYS_NO_PATHCONV=1 docker exec tsp_db pg_restore -U tsp -d tsp --clean --if-exists /tmp/restore.dump

# 3) 清理容器内临时归档（别留在可写层里白占 vhdx，见 §3）
MSYS_NO_PATHCONV=1 docker exec tsp_db rm -f /tmp/restore.dump
```

恢复前先确认归档没坏：

```bash
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "E:\tsp-backups\db:/tsp_dump_check:ro" \
  --entrypoint pg_restore postgres:16.9 -l /tsp_dump_check/tsp-db-tsp-20261008-214159.dump
```

正常输出应包含 `; Archive created at ...`、`; TOC Entries: 4`、`; Format: CUSTOM`。
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
4. 启动 Docker Desktop，`docker volume ls` 确认 `tsp_parquet` / `tsp_dbdata` 在。

---

## 6. 日常备份：一条命令

```powershell
cd E:\ai_codes\ai_personal_panel\tsp-fresh
.\scripts\db-backup.ps1              # 默认保留 14 份，自动出 D 盘副本
.\scripts\db-backup.ps1 -DryRun      # 演练，不产生任何文件
.\scripts\db-backup.ps1 -KeepBackups 30
```

六步，每步都会失败即停（`throw`）：

| 步 | 做什么 | 失败会怎样 |
|---|---|---|
| [1/6] | 预检 docker / 容器 healthy / .env 里 `POSTGRES_*` | 缺变量直接停 |
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
* `docker-compose.yml` 只写 `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD 必须在 .env 中设置}`，
  **不硬编码**；`${VAR:?...}` 语法保证忘了配就起不来，而不是静默用弱密码；
* 提交前务必 `git status --short` 确认没有密码文件被 stage。

```bash
git check-ignore -v .env      # 应输出 .gitignore:55:.env
git status --short            # 不应出现 .env
```

---

## 8. 例行检查清单

**每次动 compose / 卷之后：**

- [ ] `docker volume ls | grep tsp` —— `tsp_parquet`、`tsp_dbdata` 都在；
- [ ] `docker ps` —— `TickFlow_Stock_Panel` 和 `tsp_db` 都 Up；
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
| `scripts/db-backup.ps1` | Postgres 备份 + 异盘副本 + 保留策略 + 可恢复性校验 |
| `scripts/db-down.ps1` | 下线 db（**不带 `-v`**，显式拒绝 `-RemoveVolumes`） |
| `scripts/db-upgrade-major.ps1` | Postgres 跨大版本逻辑迁移 |
| `scripts/deploy.ps1` / `rollback.ps1` | TSP 应用发布 / 回滚 |
| `scripts/export-parquet.ps1` | parquet 导出 |
| `scripts/deploy-common.ps1` | 公共函数库（输出、命令执行、compose 调用） |
