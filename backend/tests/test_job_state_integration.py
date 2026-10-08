"""S4 切片①: 真连 Postgres 的集成测试(**默认跳过**)。

用途: 验证 DDL 与 psycopg3 适配在真实 PG 上成立 —— 单元测试里的假写入器
证明不了「SQL 真的能执行」。

怎么跑(需先确认 postgres-18.6 在跑、且 `.env` 里 POSTGRES_* 正确):
    cd backend
    TSP_POSTGRES_ENABLED=true \
    ./.venv/Scripts/python.exe -m pytest tests/test_job_state_integration.py -q -p no:cacheprovider

`host.docker.internal` 解析不通时用 TSP_POSTGRES_HOST=localhost 覆盖
(环境变量优先级高于 .env)。

清理: 用例只删自己插入的行。表本身是切片的产物, 保留;
要彻底回退见设计文档 §8 的 L3。
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.state import db, schema

pytestmark = pytest.mark.skipif(
    not settings.tsp_postgres_enabled,
    reason="集成测试需显式 TSP_POSTGRES_ENABLED=true",
)

_TEST_PREFIX = "itest"


@pytest.fixture
def conn():
    db.reset_for_tests()
    assert db.init(), f"db.init() 失败: {db.db_status()['reason']}"
    with db.connection() as c:
        schema.ensure_schema(c)
        # 必须提交: 否则本连接持有 job_run 的 DDL 锁, 写线程的 UPSERT 会一直等到
        # statement_timeout(5s) 才失败。这是集成测试自己的坑, 不是生产行为。
        c.commit()
        yield c
    db.close()
    db.reset_for_tests()


def _delete(c, *ids: str) -> None:
    for i in ids:
        c.execute("DELETE FROM job_run WHERE id = %s", (i,))
    # 立即提交: 用例失败时 `with connection()` 会回滚, 清理不能跟着回滚掉
    c.commit()


def test_schema_creates_job_run(conn):
    cur = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_name = 'job_run' ORDER BY ordinal_position"
    )
    cols = {r[0]: r[1] for r in cur.fetchall()}
    assert "id" in cols and "status" in cols
    assert cols["started_at"] == "timestamp with time zone"
    assert cols["result"] == "jsonb"
    assert cols["duration_s"] == "double precision"

    cur = conn.execute("SELECT version FROM tsp_schema_migration")
    assert schema.SCHEMA_VERSION in {r[0] for r in cur.fetchall()}


def test_job_lifecycle_roundtrip(conn, tmp_path):
    """真跑一遍 JobStore 全生命周期, 确认镜像行真的落库且字段正确。"""
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create(job_type="market_daily", market="HK")
    try:
        store.start(jid)
        store.progress(jid, "fetch", 7, "第一批")
        store.succeed(jid, {"symbols_total": 2840, "completed_symbols": ["s"] * 2617,
                            "failed_symbols": ["f"] * 223})
        assert db.drain(timeout=5.0), "镜像写未在 5s 内落库"

        cur = conn.execute(
            "SELECT status, job_type, market, rows_total, rows_done, rows_failed,"
            " duration_s, result_trimmed, result_overflow, started_at, finished_at"
            " FROM job_run WHERE id = %s",
            (jid,),
        )
        row = cur.fetchone()
        assert row is not None, "镜像写没有落库"
        status, jt, market, total, done, failed, dur, trimmed, overflow, started, finished = row

        assert status == "succeeded"
        assert (jt, market) == ("market_daily", "HK")
        assert (total, done, failed) == (2840, 2617, 223)
        assert trimmed is True and overflow is False      # 大数组被剔掉了
        assert dur is not None and dur >= 0
        # 时区: 落库后读回仍是 UTC(容器 TZ=Asia/Shanghai, server 是 UTC)
        assert started is not None and started.utcoffset().total_seconds() == 0
        assert finished >= started
    finally:
        _delete(conn, jid)


def test_reconcile_marks_orphan_running(conn):
    orphan = f"{_TEST_PREFIX}_orphan"
    try:
        conn.execute(
            "INSERT INTO job_run (id, status, started_at) VALUES (%s, 'running', now())",
            (orphan,),
        )
        assert schema.reconcile_interrupted(conn) >= 1
        cur = conn.execute("SELECT status, error_kind FROM job_run WHERE id = %s", (orphan,))
        assert cur.fetchone() == ("failed", "interrupted")
    finally:
        _delete(conn, orphan)
