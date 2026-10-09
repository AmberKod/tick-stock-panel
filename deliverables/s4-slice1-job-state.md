# S4 切片①：job 执行状态入库（设计文档）

- 作者：高见远（架构师）
- 日期：2026-10-08
- 状态：**设计稿，未实现**。本文档不含任何业务代码改动。
- 范围：把 `pipeline_jobs.JobStore` 的 job 执行记录镜像进 Postgres，作为 S4 状态层迁移的第一刀。

---

## 0. 与任务简报的事实核对（先看这里，有两条不符）

| 简报说法 | 实测结论 | 处置 |
|---|---|---|
| `.env` 里 `POSTGRES_PORT=15432`、`POSTGRES_HOST=host.docker.internal` | **.env 里确实正确**（`E:/ai_codes/ai_personal_panel/tsp-fresh/.env:51-55`） | 采信 |
| TSP 容器经 `host.docker.internal:15432` 访问 | **连通性 OK**：容器内 `host.docker.internal` 解析为 `192.168.65.254`（IPv4）+ `fdc4:f303:9324::254`（IPv6），TCP 15432 三次握手成功 | 采信，但有 IPv6 隐患，见 §7.3 |
| apscheduler 用 `AsyncIOScheduler` + `MemoryJobStore` | **正确**：`jobs/daily_pipeline.py:1966` `AsyncIOScheduler(timezone="Asia/Shanghai")`，未传 `jobstores=`，即默认 `MemoryJobStore` → 重启即丢、不产生 misfire | 采信 |
| `pipeline_jobs.py:35` 有 `LONG_JOB_TIMEOUT_S=1800`，另有 12h 硬上限 | **正确**：`services/pipeline_jobs.py:34-38`（`DEFAULT=1200` / `LONG=1800` / `HARD_JOB_TIMEOUT_S=12*3600`） | 采信 |
| 「落后 >7 自然日不补跑」闸门 | **正确**：`jobs/daily_pipeline.py:1592` `_MARKET_DAILY_CATCHUP_MAX_STALE_DAYS = {"HK": 7, "US": 7}`，判定在 `daily_pipeline.py:1669` | 采信 |
| Postgres 实例 `postgres-18.6`，库 `tsp`，0 张表 | **正确**：`docker exec postgres-18.6 psql -U tsp -d tsp -c "\dt"` → `Did not find any tables.`，版本 `PostgreSQL 18.6` | 采信 |
| 应用代码零数据库依赖 | **正确**：容器内 `asyncpg / psycopg / sqlalchemy` 全部 `MISSING`；`backend/.venv` 同样全部 `MISSING` | 采信 |
| ⚠️ 「改了 `.env` 必须重建容器才生效」 | **这一条只对 `os.environ` 成立，对 `settings` 不成立，而且是本切片的致命陷阱** | 见下 |
| ⚠️ 未提及 | **正在运行的 `TickFlow_Stock_Panel` 容器内 `POSTGRES_PORT=5432`、`POSTGRES_HOST` 根本不存在** | 见下 |

### 0.1 陷阱一：线上容器的 Postgres 环境变量是**过期**的

```
$ docker inspect TickFlow_Stock_Panel --format '{{.Created}}'
2026-10-08T14:57:05Z        # 本地 22:57
$ ls -l .env
Oct  8 23:18               # 比容器创建晚 21 分钟

$ docker exec TickFlow_Stock_Panel env | grep POSTGRES
POSTGRES_PASSWORD=kNzw...
POSTGRES_USER=tsp
POSTGRES_PORT=5432         # ← 过期，.env 里现在是 15432
POSTGRES_DB=tsp
                           # ← POSTGRES_HOST 整个不存在
```

**结论**：任何用 `os.environ["POSTGRES_HOST"]` / `os.getenv("POSTGRES_PORT")` 取配置的代码，在线上容器里都会拿到「无 host + 端口 5432」，必然连不上，且**这个失败在开发环境完全复现不出来**（开发机读的是正确的 .env）。

### 0.2 陷阱二：`settings` 读的是**实时**的 `/app/.env`，不受 env_file 固化影响

- `Dockerfile:124`：`ENV TICKFLOW_ENV_FILE=/app/.env`
- `docker-compose.yml:45`：`- ./.env:/app/.env:ro`（bind mount，**实时反映宿主机 .env**）
- `backend/app/config.py:67-73`：`_ENV_FILE = os.environ.get("TICKFLOW_ENV_FILE", .../.env)`
- `backend/app/config.py:157`：`settings = Settings()`（pydantic-settings，读 `_ENV_FILE`）

实测容器内 `/app/.env` 已是最新：`POSTGRES_HOST=host.docker.internal`、`POSTGRES_PORT=15432`。

**设计硬约束（写进代码注释）**：DB 配置**只能**通过 `app.config.settings` 读取，**禁止**用 `os.environ` 直取。这样即使不重建容器，配置也是对的。

### 0.3 附带发现：应用数据卷不是 bind mount

`docker-compose.yml:42` 用的是 `tsp_parquet:/app/data`（named volume，顶层 `external: true`），不是宿主机 bind mount。宿主机 `tsp-fresh/data/` 是一份**平行副本**（compose 注释第 41 行明确「保留为平行副本，不再挂载」）。

含义：`data/job_store/*.json` 的**权威副本在 Docker volume 里**，宿主机上看不到线上真实的 job 历史。这也是「查历史只能 `docker exec`」的根源之一。

---

## 1. 现状：job 状态到底存在哪

### 1.1 唯一权威：`pipeline_jobs.JobStore`

文件：`backend/app/services/pipeline_jobs.py`

| 数据 | 存放位置 | 代码位置 |
|---|---|---|
| pending / running 的 job 全量字段 | **进程内存** `JobStore._active_jobs: dict` | `pipeline_jobs.py:104` |
| 当前活跃 job 指针（单飞用） | **进程内存** `JobStore._active_id` | `pipeline_jobs.py:105` |
| 取消标志 | **进程内存** `_CANCEL_FLAGS: dict[str, threading.Event]`（上限 32 条，淘汰最老） | `pipeline_jobs.py:62-63` |
| 重任务互斥执行槽 | **进程内存** `_run_slot_owner: str \| None` | `pipeline_jobs.py:452` |
| succeeded / failed 的 job 全量字段 | **磁盘** `{data_dir}/job_store/{id}.json` | `pipeline_jobs.py:111-120`（`_write_file`） |
| 磁盘保留上限 | **50 个文件**，超了删最老 | `pipeline_jobs.py:133-144`（`_delete_oldest`） |

**关键**：`_write_file()` **只在 `succeed()`（:235）和 `fail()`（:249）里被调用**。也就是说——

> **pending / running 状态的 job 从来不落盘。**

### 1.2 重启容器后丢什么 / 不丢什么

| 丢 | 不丢 |
|---|---|
| 所有 `pending`/`running` 的 job 记录（含 `log`、`result`、`progress`） | 已终态（succeeded/failed）的 job，最多 50 条 |
| `_active_id`、取消标志、执行槽持有者 | parquet 行情数据（在 `tsp_parquet` volume 里） |
| 正在跑的 executor 线程本身 | 用户配置（`preferences`） |

推论：**「跑到 60% 时重启」这条记录永久消失，没有任何痕迹**。重启后 `JobStore` 是空的，`/api/pipeline/jobs` 只返回磁盘上那 50 条终态记录。

### 1.3 现在想查「跑了多久 / 成功失败 / 多少条」怎么查

只有两条路，都很差：

1. **API**：`GET /api/pipeline/jobs?limit=N`（`api/pipeline.py:114-119`）
   - `list_recent()` 走 `_summary()`（`pipeline_jobs.py:404-416`），**summary 把 `log` 字段整个丢掉**，只留 12 个字段。
   - 上限 50 条，实测覆盖 **Oct 2 07:15 → Oct 8 23:00，约 6 天**。
   - 无筛选、无聚合、无时间窗查询。
   - `GET /api/pipeline/jobs/{id}`（`api/pipeline.py:91-99`）能拿到单条全量（含 log），但**你必须先知道 job_id**。
2. **翻文件**：`docker exec TickFlow_Stock_Panel cat /app/data/job_store/<id>.json`。

**聚合类问题（成功率、平均耗时、p95）目前根本无法回答。**

### 1.4 线上 job_store 实测画像

```
50 个文件（已满上限）
status:  succeeded 34 / failed 16
result 形状:
  33 条  instruments_sync    {status, completed_symbols, failed_symbols, universe_sync_rows, market_timezone}
   1 条  market_daily        {28 个 key，含 universe_symbols / failed_symbols / failures / provider_errors ...}
  16 条  failed              result = null（失败时 result 被丢弃，只剩 error 字符串）
最大文件: 3.68 MB —— market_daily 全量同步，duration_s=5897（98 分钟），
          universe_symbols 2840 个，failed_symbols 223 个，log 只有 3 条
```

两个必须处理的现实：
- **`result` 里塞了整个标的池**（2840 个 symbol），单条 3.68 MB。直接塞 JSONB 会很难看。
- **失败的 job `result=null`**，只有 `error` 一个字符串——所以「失败原因」字段必须单独成列，不能指望从 result 里挖。

### 1.5 已有的「状态持久化」抽象层（可复用，但都是文件级）

| 模块 | 机制 |
|---|---|
| `services/json_report_store.py:27` `JsonReportStore` | 原子写（tmp + `os.replace`）+ 实例锁 + 保留上限。AI 报告三类共用的底座 |
| `services/mining_jobs.py:123` `MiningRunStore` | `data/mining_runs/<run_id>/{manifest,summary}.json` + `events.jsonl`；有 `_atomic_write_json`、显式状态机 `_transition_locked`（:411）、`recover_interrupted()`（:381） |
| `services/market_daily_sync.py:108` | `data/checkpoints/market_daily/{job_id}.json` 断点续跑 |
| `services/novel_store.py:727` / `novel_rewrite_store.py:1712` | `books/<id>/checkpoints/*.json`，Step 级状态机，原子写 |

**共同点**：清一色「JSON 文件 + 原子写 + 保留上限 + 显式状态机」。**没有**任何 DB 层、没有仓储抽象、没有 ORM。

**结论**：这一层不能直接复用（它是文件语义），但**状态机 + 显式 unavailable 原因码**的写法要照搬。最近的样板是 `daily_pipeline.py:1603-1618` 的 `_CATCHUP_REASON_*`（`no_data` 绝不冒充 `fresh`）——这正是本项目 fail-closed 纪律的标准姿势，§5 会照抄。

### 1.6 前端消费点

- `frontend/src/pages/Data.tsx:85-92` 主列表（`pipelineJobs(15)`）+ 单条轮询
- `frontend/src/pages/Dashboard.tsx:219,263` 同步按钮进度
- `frontend/src/components/Layout.tsx:392-394` 全局 `pipelineJobs(1)` 心跳
- `frontend/src/components/data/MarketDailySyncPanel.tsx:39`、另有 `EnrichedRebuildPanel` / `ExtendHistoryPanel` / `RepairDailyPanel` / `MinuteSyncConfig` 各轮询

前端只认 `PipelineJob` 这一种形状——**切片①不改这个形状**，保证前端零回归。

---

## 2. 切片①的范围边界

### 2.1 切什么

| # | 内容 |
|---|---|
| 1 | `app/state/db.py`：psycopg3 连接池 + 配置 + 健康状态 + 熔断器 |
| 2 | `app/state/schema.py`：建表 DDL + `ensure_schema()`（幂等） |
| 3 | `app/state/job_state.py`：`JobStore` → DB 的**单向写镜像**（create / start / progress / succeed / fail）+ 计数抽取 + 裁剪 |
| 4 | `JobStore` 五个方法各插一个镜像写调用（改动 ~15 行） |
| 5 | `create()` 增加 `job_type` 可选参数（默认 `"pipeline"`，向后兼容 8 个现有调用点） |
| 6 | 新 API：`GET /api/pipeline/history`、`GET /api/pipeline/stats` |
| 7 | `GET /api/pipeline/jobs` 响应增加 `history_db` 状态块（显式声明 unavailable） |
| 8 | 启动时 reconcile：把上次进程遗留的 `running` 行标为「进程重启中断」 |
| 9 | 测试：单元测试 + 熔断测试 + 可选集成测试 |

### 2.2 不切什么（明确排除，留给后续切片）

| 不切 | 原因 |
|---|---|
| 行情 parquet / DuckDB 数据 | 量级完全不同，S4 后续切片 |
| 用户配置 `preferences.json` / `auth.json` / `secrets.json` | 读多写少、强一致要求低 |
| `MiningRunStore` / novel checkpoints / market_daily checkpoints | 各自有完整状态机，迁移收益低于风险；等切片①验证 DB 层稳定后再动 |
| apscheduler 的 `MemoryJobStore` → `SQLAlchemyJobStore` | 换 jobstore 会改变 misfire 语义，是行为变更不是镜像；风险高 |
| 取消标志 / 执行槽（`_CANCEL_FLAGS`、`_run_slot_owner`） | **纯进程内同步原语，必须留在内存**。它们管的是「线程还在不在」，DB 管不了也绝不能管 |
| DB 成为 job 状态的**权威源** | 见 §5.1，这是本设计的核心决定 |
| 前端历史面板 | 列为 ①b，可独立裁剪（见 §9） |

### 2.3 一句话边界

> **JSON 文件仍是权威源，Postgres 是「写后镜像 + 历史查询加速器」。DB 挂了，同步照跑，只是查不到长历史。**

---

## 3. 驱动选型：psycopg 3（`psycopg[binary,pool]`）

### 3.1 决定性理由

先看 `JobStore` 的方法被**谁**调用：

| 方法 | 调用线程 | 证据 |
|---|---|---|
| `progress()` | **worker 线程**（手动触发路径）；**事件循环线程**（调度路径） | `api/pipeline.py:59-61` 的 `progress` 闭包在 `_run()` 里跑，而 `_run` 交给 `loop.run_in_executor(_long_task_executor, ...)`（:69）；`daily_pipeline.py:926` 的闭包在 `fn(on_progress=...)` 里跑，而 `fn` 由 APScheduler `AsyncIOExecutor` 执行 |
| `start()` / `succeed()` / `fail()` | **事件循环线程**（两条路径都是） | `api/pipeline.py:56,70,79`（async `task()` 内）；`daily_pipeline.py:930,932,940`（`_run_tracked` 内） |

**写入来自两条不同线程路径。**

- **asyncpg**：连接池绑定 event loop。worker 线程要写得 `asyncio.run_coroutine_threadsafe(...).result()`——需要存 loop 引用、两套代码路径，且 worker 线程阻塞等 loop 时若 loop 正忙有死锁风险。
- **psycopg3 同步 API + `psycopg_pool.ConnectionPool`**：池本身线程安全，一套代码路径通吃。
- **SQLAlchemy Core/ORM**：1 张表、1 人团队、无对象关系映射需求 → 纯负担。等表数量到 5+ 张、或出现需要版本化迁移时再引入（见 §4.4）。

**一句话理由**：job 状态写入横跨 worker 线程与事件循环两条路径，psycopg3 的同步连接池线程安全、一套代码即可覆盖，而 asyncpg 需要 loop 亲和的双路径 plumbing——在「每小时几十次写入」的量级上，asyncpg 的性能优势换不来任何收益，只换来实现复杂度和死锁面。

### 3.2 依赖写法

```toml
# backend/pyproject.toml [project].dependencies
"psycopg[binary,pool]>=3.2,<4",
```

用 `[binary]` 避免编译工具链依赖；`<4` 上限与本项目对 polars 的一贯做法一致（见 pyproject 里 polars 的注释）。

### 3.3 写入线程模型

不阻塞事件循环、也不阻塞 worker 线程：

```python
# app/state/db.py
_db_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")

def submit(fn, *args) -> None:
    """统一投递到单写线程: 事件循环与 worker 线程都走这条路。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _db_writer.submit(fn, *args)        # worker 线程: 直接投递
    else:
        loop.run_in_executor(_db_writer, fn, *args)   # 事件循环: 不阻塞

def _submit_sync(fn, *args) -> None:
    _db_writer.submit(fn, *args)
```

`max_workers=1` 顺带序列化所有写（无池争用，`max_size=2` 足够）。

---

## 4. Schema 设计

### 4.1 建表 DDL（`app/state/schema.py`）

```sql
CREATE TABLE IF NOT EXISTS job_run (
    -- 标识
    id                text        PRIMARY KEY,          -- uuid4().hex[:10]，与 JSON 文件名同源
    job_type          text        NOT NULL DEFAULT 'pipeline',
    market            text,                              -- CN / HK / US / NULL(跨市场)
    trigger_source    text        NOT NULL DEFAULT 'manual',  -- manual / scheduled / catchup
    host              text,                              -- 产生该 job 的容器/进程标识
    app_version       text,

    -- 状态
    status            text        NOT NULL,              -- pending / running / succeeded / failed
    stage             text,
    progress          smallint,
    stage_pct         smallint,
    timeout_s         integer,                           -- reap 停滞阈值（1200 / 1800）

    -- 时间（全部 timestamptz，UTC 存储）
    started_at        timestamptz,
    last_progress_at  timestamptz,
    finished_at       timestamptz,
    duration_s        double precision,                  -- 跑了多久
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    -- 处理量（**未知必须是 NULL，绝不能是 0**）
    rows_total        integer,
    rows_done         integer,                           -- 处理了多少条
    rows_failed       integer,

    -- 失败原因
    error             text,                              -- 失败原因（failed 时 result 为 null，只靠它）
    error_kind        text,                              -- timeout / cancelled / provider / unknown

    -- 明细（裁剪后）
    result            jsonb,                             -- 已剔除 universe_symbols 等大数组
    log               jsonb,                             -- 最近 N 条进度日志
    result_trimmed    boolean     NOT NULL DEFAULT false, -- 显式声明"被裁剪过"
    result_overflow   boolean     NOT NULL DEFAULT false, -- 显式声明"超限丢弃"（不静默给空）

    CONSTRAINT job_run_status_chk
        CHECK (status IN ('pending','running','succeeded','failed'))
);

CREATE INDEX IF NOT EXISTS job_run_started_idx       ON job_run (started_at DESC);
CREATE INDEX IF NOT EXISTS job_run_status_started_idx ON job_run (status, started_at DESC);
CREATE INDEX IF NOT EXISTS job_run_type_started_idx   ON job_run (job_type, started_at DESC);

-- 迁移账本（替代 Alembic，见 §4.4）
CREATE TABLE IF NOT EXISTS tsp_schema_migration (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
```

### 4.2 字段回答能力对照

| 用户问题 | 字段 |
|---|---|
| 跑了多久 | `duration_s`（秒，两位小数）；或 `finished_at - started_at` |
| 成功 / 失败 | `status` + `error_kind` |
| 处理多少条 | `rows_done` / `rows_failed` / `rows_total` |
| 失败原因 | `error`（原文）+ `error_kind`（分类） |
| 哪一类任务 | `job_type` + `market` |
| 是手动还是调度 | `trigger_source` |
| 卡在哪一步 | `stage` + `stage_pct` + `log` |
| 被重启打断了吗 | `status='failed' AND error_kind='interrupted'`（见 §6） |

### 4.3 计数抽取规则（`rows_total` / `rows_done` / `rows_failed`）

按 `job_type` 分派，**未识别类型一律 `(NULL, NULL, NULL)`**：

| job_type | total | done | failed |
|---|---|---|---|
| `market_daily` | `result["symbols_total"]` 或 `len(universe_symbols)` | `len(completed_symbols)` | `len(failed_symbols)` 或 `len(failures)` |
| `pipeline` | `NULL` | `result["universe_size"]` | `result["lagging_symbols"]` |
| `minute_sync` | `NULL` | `result["universe_size"]` | `NULL` |
| `instruments_sync` | `NULL` | `result["universe_sync_rows"]` | `NULL` |
| 其他 / result 非 dict | **`NULL`** | **`NULL`** | **`NULL`** |

> **纪律**：抽不出来就写 `NULL`，**绝不写 0**。`0` 会被读成「确认处理了 0 条」，那是假数据。本项目有过 `mirror-counted-zero` 的前车之鉴（`deliverables/qa-footer-05-mirror-counted-zero.png`）。前端/API 展示时 `NULL` 渲染为 `—`。

### 4.4 迁移策略：不用 Alembic

- `ensure_schema()` 在启动时执行：建表 + `CREATE INDEX IF NOT EXISTS` + 后续 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`，全部幂等。约 40 行。
- 版本记在 `tsp_schema_migration`，只用于**观测**（启动日志打印已应用版本），不做自动升级编排。
- **什么时候上 Alembic**：表 ≥ 5 张，或出现第一个「必须 ALTER 列类型 / 删列 / 回填」的破坏性迁移。切片①不满足，上了就是过度工程。

### 4.5 时区处理（易错点）

`JobStore` 存的时间戳形如 `"2026-10-08T15:00:29Z"`（`pipeline_jobs.py:217`，`datetime.utcnow()` + 手工拼 `Z`），是 **UTC 字符串**。

- Postgres 侧列类型是 `timestamptz`，但 **PG server 时区是 UTC，容器 `TZ=Asia/Shanghai`（Dockerfile:141）**——两者不一致。
- 因此**必须**在写入前把字符串解析成 **tz-aware datetime** 再交给 psycopg，不能让 psycopg/pg 去猜。
- 现成的轮子：`pipeline_jobs.py:482` 已有 `_parse_utc(ts)`（`fromisoformat(ts.replace("Z","+00:00"))`）。直接复用，不要在 repo 层重写。
- `duration_s` 继续沿用 `_duration_s()`（`pipeline_jobs.py:419`），不另算，保证与 JSON 侧完全一致。

---

## 5. 迁移策略与 fail-closed

### 5.1 核心决定：DB 不是权威源

**`_active_jobs` / `_active_id` / `_CANCEL_FLAGS` / `_run_slot_owner` 继续留在内存，DB 不参与任何「这个 job 还活着吗」的判断。**

理由：
1. 它们是**进程内的线程同步原语**，语义是「执行体还在不在」，DB 表达不了。
2. 反过来做（DB 判定 active）会引入致命回归：`reap_stale` 刚把卡死 job 标 failed、僵尸线程还在写盘时，一条陈旧 DB 行可能让新进程误判「有任务在跑」而拒绝服务——而此时**根本没有线程在跑**，执行槽永远没人释放。
3. 单飞语义（`create()` 的 `is_new`）必须保持纯内存，否则 DB 抖动会直接破坏防并发保护。

### 5.2 DB 不可用时，job 怎么办：**照跑**

完整决策表：

| 场景 | job 行为 | 对外声明 |
|---|---|---|
| DB 可用 | 正常跑，终态与关键进度入库 | `history_db.status = "ok"` |
| 启动时连不上 | 正常启动，正常跑；`ensure_schema()` 跳过 | 启动日志 `WARNING db=unavailable reason=...`；`history_db.status="unavailable"` |
| 运行中 DB 挂了 | **当前 job 继续跑、照常落 parquet、照常 succeed/fail**；镜像写失败只记 WARN | `history_db.status="unavailable"` + `reason` + `since` + `dropped_writes` 计数 |
| 前端轮询 | `GET /api/pipeline/jobs` **照常返回实时进度**（来自内存/JSON） | 响应里带 `history_db` 块显式告知「长历史当前不可查」 |
| `GET /api/pipeline/history` | 降级：回退读本地 JSON（≤50 条） | `{available: false, reason: "...", source: "local_json", items: [...]}`——**标签明确，绝不冒充完整历史** |

**绝不发生的事**：
- DB 挂了 → 同步失败 / `/run` 返回 503 —— ❌ 不允许
- DB 挂了 → history 返回空数组且不说明原因（读作「最近没有失败」）—— ❌ 不允许
- DB 没配 → 静默跳过、日志无痕 —— ❌ 不允许

这就是本项目的铁律：**不可用必须显式声明，绝不静默给假数据**。

### 5.3 熔断器（防止 DB 故障拖慢主流程）

```python
# app/state/db.py 内部状态
_status: Literal["disabled", "ok", "unavailable"] = "ok"
_reason: str = ""
_since: float = 0.0
_open_until: float = 0.0        # 熔断窗口
_dropped_writes: int = 0
```

- 连续 3 次写失败 → 熔断，`_open_until = now + 60s`。
- 熔断窗口内的写请求**直接丢弃**（不建连、不等待），只累加 `_dropped_writes`。
- 窗口过后下次写尝试时**顺带做一次探活**（`SELECT 1`），成功则复位 `ok`。
- 读路径同理，读失败也会触发探活。
- 参数：`connect_timeout=3`、`statement_timeout=5000`（毫秒）、`pool max_size=2, min_size=0, timeout=3`。

`connect_timeout=3` 是硬要求：避免 `host.docker.internal` 解析到不可路由的 IPv6 时把写线程挂死（见 §7.3）。

### 5.4 双写顺序

**先写本地（内存/JSON），再异步投 DB。**

```
succeed(job_id, result):
    with self._lock:           # 本地权威写（不变）
        ...
        self._write_file(j)    # JSON
    job_state.mirror(j)        # 锁外，异步投 DB，永不返回异常、永不阻塞
```

`mirror()` 内部 `try/except Exception` 全兜，异常只进日志 + 熔断计数，**绝不向上抛**。这样即使 `job_state` 全盘 bug，也只是「没有历史」，不会让同步失败。

### 5.5 灰度开关（无需重建容器的 kill switch）

- `.env` 增加 `TSP_POSTGRES_ENABLED=true|false`。
- 通过 `settings` 读取 → **改 `.env` 即时生效，不需要重建容器**（见 §0.2）。
- `false` 时：不建池、不建表、不写、不探活；`history_db.status="disabled"`；`/history` 直接回退本地 JSON。
- 这是**第一道回退手段**，比 `git revert` 快得多（见 §8）。

---

## 6. 连接管理与生命周期

### 6.1 配置（`app/config.py` 新增）

```python
# Postgres (S4 切片①: job 执行状态镜像)
# ⚠️ 只能经 settings 读取, 禁止 os.environ 直取 ——
#    线上容器的进程 env 是容器创建时 env_file 固化下来的快照, 可能已过期
#    (实测: 容器内 POSTGRES_PORT=5432 且无 POSTGRES_HOST, 而 /app/.env 是实时 bind mount, 正确)。
postgres_enabled: bool = True
postgres_host: str = "host.docker.internal"
postgres_port: int = 15432
postgres_user: str = "tsp"
postgres_password: str = ""
postgres_db: str = "tsp"
postgres_connect_timeout_s: float = 3.0
```

命名用 `TSP_POSTGRES_ENABLED` 以获得独立前缀（pydantic-settings 大小写不敏感映射 `POSTGRES_HOST` → `postgres_host` 依然成立）。

### 6.2 生命周期挂钩点

`backend/app/main.py` 的 `_application_lifespan`（:95）：

**启动**（插在 `app.state.repo` 初始化之后、调度器启动之前，约 main.py:113）：

```python
try:
    from app.state import db as state_db
    app.state.db = state_db.init()      # 建池 + 探活 + ensure_schema + reconcile
except Exception as e:
    logger.warning("db state layer unavailable: %s", e)   # 启动**不失败**
    app.state.db = None
```

**关闭**（插进 `finally:` 块，main.py:379-403，`scheduler.shutdown` 之前）：

```python
db = getattr(app.state, "db", None)
if db:
    db.close()      # pool.close(timeout=2.0) + _db_writer.shutdown(wait=False)
```

`min_size=0`：服务空闲时不占连接。池大小 `max_size=2`（单写线程 + 偶尔的读）。

### 6.3 `host.docker.internal` 解析失败怎么办

分三层，逐层降级：

1. **启动预检**（`init()` 内）：`socket.getaddrinfo(host, port)` + TCP 连接，把解析出的**全部地址**打进日志。
   ```
   db preflight: host=host.docker.internal:15432 addrs=['192.168.65.254', 'fdc4:f303:9324::254'] tcp=ok
   ```
   解析失败 / 连接失败 → `WARNING db=unavailable reason=...`，**继续启动**。
2. **psycopg 侧**：`connect_timeout=3`，psycopg 按 `getaddrinfo` 顺序逐个试，IPv6 不通会自动退到 IPv4。已经过实测验证（IPv4 `192.168.65.254` 三次握手成功）。
3. **人工兜底（写进文档，不写进代码）**：若 Docker 重启后 `host.docker.internal` 的 IPv4 变化导致不通，`.env` 里把它改成实测 IPv4（当前 `192.168.65.254`）。改 `.env` 走 `settings` 实时生效，不必重建容器。

**绝不**在代码里做「解析失败就 fallback 到 127.0.0.1」这种事——容器内 127.0.0.1 是容器自己，连过去失败还算好的，万一有监听就会写错库。

### 6.4 启动时 reconcile

`init()` 末尾执行一次（事务内）：

```sql
UPDATE job_run
   SET status='failed',
       error='进程重启中断(未观察到终态)',
       error_kind='interrupted',
       finished_at = COALESCE(finished_at, now()),
       updated_at  = now()
 WHERE status IN ('pending','running');
```

- 只在**单实例**前提下正确（本项目是单人自用单容器，`MiningProcessLock`（main.py:408）已按单实例设计）。
- 语义是**诚实的事实陈述**：进程重启了，这条 run 不可能还活着。它不是推断出的假状态。
- 用途：把「跑到 60% 被重启打断」这条**以前完全消失**的记录变成可见。这是 §10 里用户能感知到的第 5 条。

---

## 7. 主要风险与处置

### 7.1 🔴 `uv lock` 与镜像重建（最高风险）

- 加依赖 → 必须重新生成 `backend/uv.lock`。
- 容器 CMD 是 `uv run uvicorn ...`（Dockerfile:144），且 compose 设了 `UV_FROZEN=1`（docker-compose.yml:36）。**`uv.lock` 与 `pyproject.toml` 不一致时，`uv run` 会直接失败 → 容器起不来**。
- 历史教训：本仓库 `uv lock` 曾因 `requires-python` 无上限 + `py-mini-racer` 平台分支而**长期无法重新生成**（见 `backend/pyproject.toml:5-11` 的注释）。虽然后来加了 `<3.14` 上限并改名 `mini-racer`，但重新 lock 仍需实测。

**处置**：
1. 动手前先在本机跑 `cd backend && uv lock`，确认能生成、且 `uv sync --frozen` 通过。**这一步不通过就不要往下做。**
2. 同步确认新 lock 在 Linux 平台能解出 `psycopg[binary]`。
3. 部署才需要 `docker compose up -d --build`（会重建 `TickFlow_Stock_Panel`，短中断）。本次任务**不执行**；实现阶段由你择时。
4. 顺带好处：重建后 `env_file` 重新固化，§0.1 的过期 env 问题一并消失。

### 7.2 🔴 线上容器 env 过期（已实测，见 §0.1）

**处置**：代码只走 `settings`（读实时 `/app/.env`）；`postgres_host/port` 给安全默认值；启动预检把实际生效的 host/port 打进日志。**加一条测试**：断言 `app/state/db.py` 里不出现 `os.environ` / `os.getenv`（用 `inspect.getsource` 做静态断言，5 行）。

### 7.3 🟡 `host.docker.internal` IPv6 优先解析

实测 `getent hosts host.docker.internal` 只返回 IPv6 `fdc4:f303:9324::254`；Python `getaddrinfo` 返回 IPv4 `192.168.65.254` 在前，连接成功。

**处置**：`connect_timeout=3` + 启动预检打印全部解析地址。若某次 Docker 重启后 IPv6 不可路由，psycopg 会多耗一个 3s 超时再退到 IPv4——因为写了熔断，最多影响一条写。

### 7.4 🟡 写放大：`progress()` 回调极高频

`market_daily` 全量同步实测 5897 秒、逐标的回调。若无节流，一次同步可能产生上万次 UPSERT。

**处置**（`job_state.mirror` 内）：
- 终态（`succeed`/`fail`/`create`）**无条件写**。
- `progress` 写**节流**：距上次写 ≥10s，或 `stage` 变化，或 `progress` 跨过 5% 的整数档，三者满足其一才写。
- 预计降到每分钟 ≤6 次、单次同步 ≤600 次——对这个量级的 Postgres（实测 20 万行/s）毫无压力。

### 7.5 🟡 JSONB 体积

实测单条 `result` 3.68 MB（2840 个 `universe_symbols` + 223 个 `failed_symbols`）。

**处置**：写库前裁剪
- 剔掉 `universe_symbols` / `completed_symbols` / `failures` / `items` 这类大数组（它们的长度已抽到 `rows_*` 列）。
- `failed_symbols` 保留**前 50 个**（够定位问题），并置 `result_trimmed = true`。
- 裁剪后仍 > 256 KB → `result` 写 `NULL`，置 `result_overflow = true`。
- 两个布尔位是关键：**必须显式声明「被裁剪 / 超限」，不能让人拿到一个静默变小的 result 还以为完整**。
- `log` 沿用现有 200 条上限（`pipeline_jobs.py:282`）。

### 7.6 🟡 时区错算

见 §4.5。若把 `"...Z"` 字符串直接交给 PG 的 `timestamptz` 列，而会话时区是 `Asia/Shanghai`，会被**当成北京时间**解析，偏移 8 小时——这类 bug 极难发现。

**处置**：repo 层强制走 `_parse_utc()`（`pipeline_jobs.py:482`）转 tz-aware 后再绑定参数；加单元测试断言 `started_at` 落库后 UTC 值与源字符串一致。

### 7.7 🟢 测试基线被污染

`job_store` 是模块级单例（`pipeline_jobs.py:431`）。双写一旦默认开启，现有 3016 个测试会集体尝试连 DB。

**处置**：
- 新增 `backend/tests/conftest.py`（**当前不存在**），顶层 `os.environ.setdefault("TSP_POSTGRES_ENABLED", "false")`。pytest 保证 conftest 先于测试模块导入，因此 `app.config.settings` 实例化时读到的是 `false`。
- 集成测试用 `@pytest.mark.skipif(not os.getenv("TSP_TEST_POSTGRES_DSN"), ...)` 显式跳过。

### 7.8 🟢 `create()` 加参数的兼容性

8 个现有调用点（`api/pipeline.py:43,168,227`、`api/kline.py:995,1184,1274,1342`、`services/data_integrity.py:276`、`jobs/daily_pipeline.py:915,1211`）全部不传 `job_type`。

**处置**：`job_type` 设为**关键字参数、默认 `"pipeline"`**，零改动兼容。逐个补 `job_type` 是 0.25 人日的收尾活，可以放在切片①末尾或①b。

---

## 8. 回退方式（按代价从低到高）

| 级别 | 操作 | 生效 | 影响 |
|---|---|---|---|
| **L1 运行时开关** | `.env` 里 `TSP_POSTGRES_ENABLED=false` | 即时（`settings` 读实时 `/app/.env`，无需重建容器） | 双写全停，系统回到切片①之前的 100% 行为 |
| **L2 代码回退** | `git revert` 切片①提交 + `docker compose up -d --build` | 一次重建 | 完全回到旧行为 |
| **L3 数据回退** | `DROP TABLE job_run; DELETE FROM tsp_schema_migration WHERE version LIKE 'job_run%';` | 即时 | 只丢镜像数据；`data/job_store/*.json` 权威源 untouched |
| **L4 依赖回退** | 从 `pyproject.toml` 删 psycopg + `uv lock` + 重建 | 一次重建 | 彻底移除依赖 |

**L1 是主要手段**——这也是为什么开关必须走 `settings` 而不是 `os.environ`：出问题时你改一行 `.env` 就能止血，不必打断正在提供服务的容器。

---

## 9. 测试策略

命令（与既有基线一致）：

```bash
cd tsp-fresh/backend && ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider
```

基线：**3016 passed / 1 xfailed / 0 failed**，全量约 5 分钟。切片①的目标是这个数字只增不减。

### 9.1 新增测试文件

**`backend/tests/test_job_state_mirror.py`**（纯逻辑，不连 DB，秒级）

用注入式假写入器（`job_state.set_mirror_sink(fake)`）验证：
1. `create → start → progress → succeed` 产生正确行字典，`duration_s` 与 JSON 侧 `_duration_s()` 完全一致。
2. `failed` 时 `error` 落库、`result` 为 `NULL`、`rows_*` 为 `NULL`。
3. **计数抽取**：`market_daily` 的 2840/223 抽成 `rows_done=2840, rows_failed=223`；**未识别 job_type 抽成 `NULL` 而不是 `0`**（回归守卫，对应 §4.3 纪律）。
4. **裁剪**：含 `universe_symbols` 的 result 写库后不含该 key，且 `result_trimmed=true`；超限样本 `result=NULL, result_overflow=true`。
5. **节流**：连续 100 次 `progress` 只触发 ≤2 次写；`stage` 变化立即触发。
6. **时区**：`started_at` 转出来的 datetime 是 tz-aware UTC，与源 `"...Z"` 字符串数值一致。
7. 静态断言：`app/state/db.py` 源码中不含 `os.environ` / `os.getenv`（对应 §7.2）。

**`backend/tests/test_job_state_failclosed.py`**（核心，验证铁律）

注入一个**永远抛异常**的 sink：
1. job 依然 `succeeded`，JSON 文件依然写好 —— **DB 失败绝不影响 job 结果**。
2. `db_status()` 返回 `unavailable`，带 `reason` 与 `since`。
3. 熔断生效：连抛 3 次后第 4 次**不再调用 sink**（用调用计数断言），`dropped_writes` 递增。
4. 窗口过后恢复：`sink` 修好后下一次写成功并把状态复位为 `ok`。
5. `GET /api/pipeline/jobs` 响应含 `history_db.status == "unavailable"`。
6. `GET /api/pipeline/history` 在 unavailable 时返回 `available=false` + `source="local_json"` + `items` 非空 —— **不返回无解释的空列表**。
7. `postgres_enabled=false` 时 `history_db.status == "disabled"`，且**整个进程零 DB 调用**（用 sink 调用计数 = 0 断言）。

**`backend/tests/test_job_state_integration.py`**（可选，默认跳过）

`@pytest.mark.skipif(not os.getenv("TSP_TEST_POSTGRES_DSN"))`：真连 `postgres-18.6`，`ensure_schema()` 建表 → 插一条 → `SELECT` 读回校验字段 → `DROP`。用于验证 DDL 与 psycopg 适配在真实 PG 18.6 上成立。**不进 CI 必备**，但实现阶段应在本地跑通一次。

### 9.2 回归面

改动落在 `JobStore` 的 5 个方法 + 新模块。重点回归：
- `tests/test_job_stall_and_cancel.py`（已有，卡死判定 + 协作式取消 + 执行槽所有权）
- `tests/test_heavy_job_limiter.py`
- `tests/test_daily_pipeline_cn_catchup.py`
- `tests/test_mining_*.py`

这些用例已通过 `_reset_module_globals`（`test_job_stall_and_cancel.py:25-32`）复位模块级全局；新增的 `job_state` 全局要在同类 fixture 里一并复位。

### 9.3 明确不测

- 不做 DB 性能测试（量级差 4 个数量级，实测 20 万行/s 已足够）。
- 不测 `postgres-18.6` 本身（外部依赖，不属于本项目）。

---

## 10. 做完这一刀，你能感知到什么

### 之前（现状）

| 想问的问题 | 能不能查到 |
|---|---|
| 昨天 15:30 那次盘后同步跑了多久？ | 只能翻 `job_store` JSON，且**文件只剩 50 条**，6 天前就没了 |
| 最近一个月管道成功率多少？ | **查不到**（无聚合） |
| 上上周三那次为什么失败了？ | **查不到**（早被 `_delete_oldest` 删了） |
| 服务重启时正在跑的那次同步呢？ | **完全不存在**（pending/running 从不落盘） |
| 某次同步处理了多少标的、失败多少？ | 要 `docker exec` 进容器逐个 cat JSON |
| 平均耗时 / p95 耗时趋势？ | **查不到** |

### 之后

1. **长历史可查**：不再受 50 条上限约束。按 `status` / `job_type` / 时间窗筛选，任意回溯。
2. **成功率与耗时聚合**：`GET /api/pipeline/stats?days=30` → 成功/失败计数、成功率、`avg(duration_s)`、`p95(duration_s)`、累计 `rows_done`。
3. **失败原因可追溯**：`error` + `error_kind` 独立成列，跨重启、跨月份可查（不再像现在这样 failed 只剩一个字符串且会被删）。
4. **处理量**：`rows_done` / `rows_failed` / `rows_total` 直接回答「多少条」。抽不出来是 `—` 而不是 `0`。
5. **重启打断可见**（新增能力）：`status='failed' AND error_kind='interrupted'` —— **以前这类记录 100% 消失，现在第一次有了痕迹**。
6. **可直接用 SQL 问**：`docker exec postgres-18.6 psql -U tsp -d tsp -c "SELECT ..."` —— 不用再 `docker exec` 进 app 容器拼 JSON。
7. **DB 健康显式可见**：`/api/pipeline/jobs` 里 `history_db` 块直接告诉你「长历史现在能不能查」，不会让你把「查不到」误读成「没问题」。

### 前端（①b，可裁剪）

`frontend/src/pages/Data.tsx:85` 已有 `pipelineJobs` 查询位。新增「同步历史」面板：近 N 条 + 状态/类型筛选 + 时长与处理量列 + 失败原因展开。**这是可选的**：砍掉它，§10 的 1–7 条依然全部成立（走 API / psql）。

---

## 11. 工作量估计（不含部署与验收）

| 项 | 人日 |
|---|---|
| `pyproject` + `uv lock` + psycopg（含 §7.1 的 lock 可行性验证） | 0.25 |
| `app/state/db.py`：配置 + 池 + 健康 + 熔断 + 单写线程 + 预检 | 0.50 |
| `app/state/schema.py`：DDL + `ensure_schema()` + reconcile | 0.25 |
| `app/state/job_state.py`：镜像写 + 节流 + 计数抽取 + 裁剪 | 0.75 |
| `JobStore` 5 处挂载 + `job_type` 参数 + 8 个调用点补齐 | 0.50 |
| API：`/history`、`/stats`、`/jobs` 的 `history_db` 块 | 0.50 |
| 测试（两个新文件 + 可选集成 + 全量回归） | 0.75 |
| **小计（后端 + 测试）** | **3.00** |
| 前端「同步历史」面板（①b，可裁剪） | +0.75 |

### 结论：**与「2–3 人日」基本相符，但偏紧；算上前端会超。**

直说：

- **不含前端：2.5–3.0 人日** —— 落在区间上沿。前提是你本机 `uv lock` 一次通过（§7.1）。若 lock 卡住，光这一项就可能吞掉 0.5–1 人日。
- **含前端：约 3.75 人日** —— 超出。
- **建议**：把前端面板拆成 **切片①b**，① 只交付后端 + API + 测试。这样 ① 稳在 3 人日内，且**用户当天就能用 `psql` / curl 看到历史**（§10 的 1–7 条立即兑现），不必等前端。前端作为独立的一小块随后补，风险与 ① 完全解耦。

### 建议的落地顺序（每步都可独立验证）

1. `uv lock` 可行性验证（**先做，不通过就停**）
2. `app/state/db.py` + `schema.py` + 启动预检 → 日志里看到 `db preflight: ... tcp=ok`
3. `ensure_schema()` → `psql \dt` 看到 `job_run`
4. `job_state` 镜像 + `JobStore` 挂载 → 手动点一次同步，`psql` 里查到 1 行
5. API + 测试 → 全量回归 3016 不变
6. （①b）前端面板

---

## 12. 附：现状关键代码索引

| 内容 | 位置 |
|---|---|
| `JobStore` 类定义 | `backend/app/services/pipeline_jobs.py:100-401` |
| 活跃 job（内存） | `:104` `_active_jobs` |
| 单飞指针（内存） | `:105` `_active_id` |
| 终态落盘 | `:111-120` `_write_file` |
| 50 条上限淘汰 | `:133-144` `_delete_oldest` |
| `create()`（含 pending∨running 单飞逻辑） | `:159-209` |
| `start()` | `:211-220` |
| `succeed()`（唯一落盘点之一） | `:222-235` |
| `fail()`（唯一落盘点之二） | `:237-249` |
| `progress()`（worker 线程调用） | `:253-288` |
| `list_recent()`（走 `_summary`，丢 log） | `:300-315` |
| `reap_stale()` 停滞/硬上限双判定 | `:320-376` |
| 取消标志（内存，上限 32） | `:57-90` |
| 执行槽 + 所有权（内存） | `:434-479` |
| `_parse_utc()`（时区解析轮子，复用） | `:482-484` |
| 模块单例 `job_store` | `:431` |
| 超时常量 1200/1800/12h | `:34-38` |
| 调度器（默认 MemoryJobStore） | `backend/app/jobs/daily_pipeline.py:1966` |
| 调度任务的 job 包装 | `:901-943` `_run_tracked` |
| 启动补跑 7 日闸门常量 | `:1592` |
| 启动补跑判定 | `:1635-1702` |
| fail-closed 原因码样板（照抄风格） | `:1603-1618` |
| API 端点 | `backend/app/api/pipeline.py:28,91,102,114` |
| 生命周期 | `backend/app/main.py:94-414`（`finally` 在 `:379-403`） |
| 配置（pydantic-settings 读 `.env`） | `backend/app/config.py:67-73, 157` |
| 原子写样板（可参照） | `backend/app/services/json_report_store.py:71-77` |
| 状态机 + 中断恢复样板 | `backend/app/services/mining_jobs.py:381, 411` |
