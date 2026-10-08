"""job_run 建表 DDL + ensure_schema()（S4 切片①）。

迁移策略: **不用 Alembic**。1 张表 + 幂等 `CREATE TABLE IF NOT EXISTS` /
`ALTER TABLE ADD COLUMN IF NOT EXISTS` 就够, 上了 Alembic 是过度工程。
什么时候再上: 表 ≥5 张, 或出现第一个「必须改列类型 / 删列 / 回填」的破坏性迁移。

`tsp_schema_migration` 只做**观测账本**(启动日志打印已应用版本), 不做自动升级编排。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "job_run_v1"

# ── 建表 ────────────────────────────────────────────────────────────────
# 列设计要点:
#   - 时间是 timestamptz, 全部按 UTC 存。写入侧必须先把 "...Z" 字符串解析成
#     tz-aware datetime(见 job_state._to_dt), 否则 PG 会按会话时区(容器是
#     Asia/Shanghai, server 是 UTC)去猜, 偏 8 小时且极难发现。
#   - rows_total / rows_done / rows_failed **未知必须是 NULL, 绝不能是 0**。
#     0 会被读成「确认处理了 0 条」, 那是假数据。
#   - result_trimmed / result_overflow 是**显式声明**: 拿到 result 的人必须能
#     分辨「这就是完整结果」和「这个结果被裁剪/超限丢弃过」。
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS job_run (
    -- 标识
    id                text        PRIMARY KEY,          -- uuid4().hex[:10], 与 JSON 文件名同源
    job_type          text        NOT NULL DEFAULT 'pipeline',
    market            text,                             -- CN / HK / US / NULL(跨市场)
    trigger_source    text        NOT NULL DEFAULT 'manual',  -- manual / scheduled / catchup
    host              text,                             -- 产生该 job 的容器/进程标识
    app_version       text,

    -- 状态
    status            text        NOT NULL,             -- pending / running / succeeded / failed
    stage             text,
    progress          smallint,
    stage_pct         smallint,
    timeout_s         integer,                          -- reap 停滞阈值 (1200 / 1800)

    -- 时间(全部 timestamptz, UTC 存储)
    started_at        timestamptz,
    last_progress_at  timestamptz,
    finished_at       timestamptz,
    duration_s        double precision,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    -- 处理量(未知必须是 NULL, 绝不能是 0)
    rows_total        integer,
    rows_done         integer,
    rows_failed       integer,

    -- 失败原因(failed 时 result 为 null, 只靠这两列)
    error             text,
    error_kind        text,                             -- timeout / cancelled / provider / unknown

    -- 明细(裁剪后)
    result            jsonb,
    log               jsonb,
    result_trimmed    boolean     NOT NULL DEFAULT false,
    result_overflow   boolean     NOT NULL DEFAULT false,

    CONSTRAINT job_run_status_chk
        CHECK (status IN ('pending','running','succeeded','failed'))
);
"""

_CREATE_INDEXES = """
CREATE INDEX IF NOT EXISTS job_run_started_idx        ON job_run (started_at DESC);
CREATE INDEX IF NOT EXISTS job_run_status_started_idx ON job_run (status, started_at DESC);
CREATE INDEX IF NOT EXISTS job_run_type_started_idx   ON job_run (job_type, started_at DESC);
"""

_CREATE_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS tsp_schema_migration (
    version     text        PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now()
);
"""

# 后续加列的唯一入口。切片① 为空(首版 DDL 已含全部列), 留作模板。
_ALTER_STATEMENTS: tuple[str, ...] = ()


def ensure_schema(conn: Any) -> None:
    """幂等建表。调用方负责事务与异常。"""
    # psycopg3 对无参数查询走 simple query 协议, 允许一条串多个语句。
    conn.execute(_CREATE_TABLE)
    conn.execute(_CREATE_INDEXES)
    conn.execute(_CREATE_MIGRATION_TABLE)
    for stmt in _ALTER_STATEMENTS:
        conn.execute(stmt)
    conn.execute(
        "INSERT INTO tsp_schema_migration (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
        (SCHEMA_VERSION,),
    )


# ── 启动 reconcile ──────────────────────────────────────────────────────
# 进程重启了, 上次遗留的 pending/running 行不可能还活着 —— 这是**事实陈述**,
# 不是推断。把它标成 interrupted, 让「跑到 60% 被重启打断」这类记录第一次留下
# 痕迹(切片①之前它们 100% 消失)。
#
# 只在单实例前提下正确: 本项目是单人自用单容器, MiningProcessLock 已按单实例设计。
_RECONCILE_SQL = """
UPDATE job_run
   SET status      = 'failed',
       error       = '进程重启中断(未观察到终态)',
       error_kind  = 'interrupted',
       finished_at = COALESCE(finished_at, now()),
       updated_at  = now()
 WHERE status IN ('pending', 'running')
"""


def reconcile_interrupted(conn: Any) -> int:
    """把上次进程遗留的 pending/running 标为 interrupted。返回受影响行数。"""
    cur = conn.execute(_RECONCILE_SQL)
    return int(cur.rowcount or 0)
