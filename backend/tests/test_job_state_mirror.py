"""S4 切片①: job 状态镜像的纯逻辑测试(注入式假写入器, 全程离线)。

覆盖设计文档 §9.1 的 7 条:
  1. create→start→progress→succeed 行字段正确, duration_s 与 JSON 侧一致
  2. failed 时 error 落库 / result 为 NULL / rows_* 为 NULL
  3. 计数抽取: 未识别 job_type 抽成 NULL 而**不是 0**(§4.3 纪律回归守卫)
  4. 裁剪: 大数组剔除 + result_trimmed; 超限 → result=NULL + result_overflow
  5. 节流: 100 次 progress 只写 ≤2 次; stage 变化立即触发
  6. 时区: started_at 是 tz-aware UTC, 数值与源字符串一致
  7. 静态断言: app/state/*.py 不直接读进程环境变量
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobStore
from app.state import db, job_state


@pytest.fixture(autouse=True)
def _reset():
    """每个用例前后把 db / job_state 的模块级状态复位, 避免相互污染。"""
    db.reset_for_tests()
    job_state.set_mirror_sink(None)
    job_state._last_emit.clear()
    yield
    job_state.set_mirror_sink(None)
    job_state._last_emit.clear()
    db.reset_for_tests()


@pytest.fixture
def sink():
    """捕获所有镜像行的假写入器(替代真实 SQL)。"""
    rows: list[dict] = []
    job_state.set_mirror_sink(rows.append)
    return rows


def _drain() -> None:
    assert db.drain(timeout=5.0), "写线程未在 5s 内完成"


def _store(tmp_path, **kw) -> JobStore:
    return JobStore(store_dir=tmp_path / "jobs", **kw)


# ── 1. 全生命周期 ───────────────────────────────────────────────────────

def test_full_lifecycle_rows(tmp_path, sink):
    store = _store(tmp_path)
    jid, is_new = store.create(job_type="market_daily", market="HK", trigger_source="manual")
    assert is_new is True
    store.start(jid)
    store.progress(jid, "fetch", 10, "拉第 1 批")
    store.succeed(jid, {"symbols_total": 100, "completed_symbols": ["a"] * 90,
                        "failed_symbols": ["b"] * 10})
    _drain()

    # create / start / progress / succeed 至少各一条(progress 可能被节流合并)
    kinds = [r["status"] for r in sink]
    assert kinds[0] == "pending"
    assert "running" in kinds
    assert kinds[-1] == "succeeded"

    final = sink[-1]
    assert final["id"] == jid
    assert final["job_type"] == "market_daily"
    assert final["market"] == "HK"
    assert final["trigger_source"] == "manual"
    assert final["status"] == "succeeded"
    assert final["progress"] == 100
    assert final["rows_total"] == 100
    assert final["rows_done"] == 90
    assert final["rows_failed"] == 10
    assert final["error"] is None and final["error_kind"] is None

    # duration_s 必须与 JSON 侧 _duration_s() 完全一致(不另算)
    j = store.get(jid)
    assert final["duration_s"] == j["duration_s"] == pipeline_jobs._duration_s(j)
    assert final["started_at"] is not None and final["finished_at"] is not None


# ── 2. failed ───────────────────────────────────────────────────────────

def test_failed_row(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create(job_type="market_daily")
    store.start(jid)
    store.fail(jid, "超时自动取消: 进度停滞 1300s 超过阈值 1200s,已请求终止")
    _drain()

    row = sink[-1]
    assert row["status"] == "failed"
    assert row["error"].startswith("超时自动取消")
    assert row["error_kind"] == "timeout"
    assert row["result"] is None          # 失败时 result 被丢弃, 库里也是 NULL
    assert row["rows_total"] is None      # 抽不出来是 NULL, 不是 0
    assert row["rows_done"] is None
    assert row["rows_failed"] is None


def test_error_kind_cancelled(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create()
    store.start(jid)
    store.fail(jid, "用户手动取消")
    _drain()
    assert sink[-1]["error_kind"] == "cancelled"


# ── 3. 计数抽取(§4.3 纪律)────────────────────────────────────────────────

@pytest.mark.parametrize("job_type,result,expected", [
    ("market_daily",
     {"universe_symbols": ["s"] * 2840, "failed_symbols": ["f"] * 223},
     (2840, None, 223)),
    ("market_daily",
     {"symbols_total": 2840, "completed_symbols": ["s"] * 2617, "failed_symbols": ["f"] * 223},
     (2840, 2617, 223)),
    ("pipeline", {"universe_size": 5000, "lagging_symbols": 12}, (None, 5000, 12)),
    ("minute_sync", {"universe_size": 300}, (None, 300, None)),
    ("instruments_sync", {"universe_sync_rows": 77}, (None, 77, None)),
    ("totally_unknown", {"universe_size": 42}, (None, None, None)),
    ("pipeline", None, (None, None, None)),
    ("pipeline", "not a dict", (None, None, None)),
])
def test_extract_counts(tmp_path, sink, job_type, result, expected):
    store = _store(tmp_path)
    jid, _ = store.create(job_type=job_type)
    store.start(jid)
    store.succeed(jid, result)
    _drain()

    row = sink[-1]
    assert (row["rows_total"], row["rows_done"], row["rows_failed"]) == expected
    # 强守卫: 任何未识别/缺失的情况都必须是 None, **绝不能是 0**
    for key in ("rows_total", "rows_done", "rows_failed"):
        assert row[key] is None or row[key] > 0


# ── 4. result 裁剪(§7.5)─────────────────────────────────────────────────

def test_big_arrays_dropped_and_trimmed(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create(job_type="market_daily")
    store.start(jid)
    store.succeed(jid, {
        "status": "ok",
        "universe_symbols": [f"HK.{i:05d}" for i in range(2840)],
        "completed_symbols": [f"HK.{i:05d}" for i in range(2617)],
        "failed_symbols": [f"HK.{i:05d}" for i in range(223)],
    })
    _drain()

    row = sink[-1]
    assert "universe_symbols" not in row["result"]
    assert "completed_symbols" not in row["result"]
    assert len(row["result"]["failed_symbols"]) == 50      # 只留前 50
    assert row["result_trimmed"] is True                    # 显式声明被裁剪过
    assert row["result_overflow"] is False
    assert row["rows_total"] == 2840 and row["rows_failed"] == 223


def test_overflow_drops_result_explicitly(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create(job_type="pipeline")
    store.start(jid)
    store.succeed(jid, {"blob": "x" * (300 * 1024)})       # > 256 KB
    _drain()

    row = sink[-1]
    assert row["result"] is None                            # 超限整个丢弃
    assert row["result_overflow"] is True                   # 显式声明, 不静默给空
    assert row["result_trimmed"] is False


# ── 5. 节流(§7.4)────────────────────────────────────────────────────────

def test_progress_is_throttled(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create()
    store.start(jid)
    _drain()
    base = len(sink)

    # 100 次回调, 全部落在同一个 5% 档、同一个 stage、远小于 10s
    for i in range(100):
        store.progress(jid, "sync", 0, f"tick {i}")
    _drain()
    assert len(sink) - base == 1, "100 次同档回调只应触发 1 次写"

    # 跨过 5% 整数档 → 立即触发
    store.progress(jid, "sync", 6, "跨档")
    _drain()
    assert len(sink) - base == 2

    # stage 变化 → 立即触发(即使档位没变)
    store.progress(jid, "write", 6, "换阶段")
    _drain()
    assert len(sink) - base == 3


def test_terminal_states_always_write(tmp_path, sink):
    """终态与 create/start 无条件写, 不受节流影响。"""
    store = _store(tmp_path)
    jid, _ = store.create()
    store.start(jid)
    store.succeed(jid, {"universe_size": 1})
    _drain()
    assert [r["status"] for r in sink] == ["pending", "running", "succeeded"]


# ── 6. 时区(§4.5)────────────────────────────────────────────────────────

def test_timestamps_are_tz_aware_utc(tmp_path, sink):
    store = _store(tmp_path)
    jid, _ = store.create()
    store.start(jid)
    _drain()

    row = sink[-1]
    started = row["started_at"]
    assert isinstance(started, datetime)
    assert started.tzinfo is not None, "必须是 tz-aware, 否则 PG 按会话时区猜, 偏 8 小时"
    assert started.utcoffset().total_seconds() == 0

    src = store.get(jid)["started_at"]                 # 形如 "2026-10-08T15:00:29Z"
    assert started == datetime.fromisoformat(src.replace("Z", "+00:00"))
    assert started == started.astimezone(UTC)          # 与 UTC 数值一致


# ── 7. 静态断言(§7.2)────────────────────────────────────────────────────

def test_state_modules_do_not_read_process_env():
    """DB 配置只能走 settings, 不能直读进程环境变量(线上容器 env 已过期)。"""
    from app.state import schema

    for mod in (db, schema, job_state):
        src = inspect.getsource(mod)
        assert "os.environ" not in src, f"{mod.__name__} 不得直读 os.environ"
        assert "os.getenv" not in src, f"{mod.__name__} 不得直读 os.getenv"
