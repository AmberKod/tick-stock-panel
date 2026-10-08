"""S4 切片①: fail-closed 铁律测试(全程离线, 注入永远抛异常的 sink)。

本项目铁律 —— 不可用必须显式声明, 绝不静默给假数据。这一组用例就是它的守卫:
  - DB 挂了 → job 照跑、照常落 JSON、照常 succeed/fail
  - DB 挂了 → /history 回退本地 JSON 但**必须带 available=false + reason**
  - DB 挂了 → 熔断丢弃写, 不建连、不等待
  - DB 没开 → 整个进程零 DB 调用, 且 status 显式为 disabled
"""
from __future__ import annotations

import json
import time

import pytest

from app.config import settings
from app.services.pipeline_jobs import JobStore
from app.state import db, job_state


@pytest.fixture(autouse=True)
def _reset():
    db.reset_for_tests()
    job_state.set_mirror_sink(None)
    job_state._last_emit.clear()
    yield
    job_state.set_mirror_sink(None)
    job_state._last_emit.clear()
    db.reset_for_tests()


def _drain() -> None:
    assert db.drain(timeout=5.0), "写线程未在 5s 内完成"


def _counter():
    """永远抛异常的 sink, 记录被调用次数。"""
    calls: list[int] = []

    def _boom(row: dict) -> None:
        calls.append(1)
        raise RuntimeError("simulated db down")

    job_state.set_mirror_sink(_boom)
    return calls


def _simulate_db_down(monkeypatch) -> None:
    """把 db 置成「熔断中的 unavailable」—— 不去碰真实实例, 也不会被写成功复位。"""
    monkeypatch.setattr(db, "_status", "unavailable")
    monkeypatch.setattr(db, "_reason", "simulated db down")
    monkeypatch.setattr(db, "_since", 1.0)
    monkeypatch.setattr(db, "_open_until", time.monotonic() + 60.0)


def _job_row(jid: str, **kw) -> dict:
    row = {"id": jid, "status": "succeeded", "stage": "done", "progress": 100,
           "stage_pct": 0, "log": [], "started_at": None, "last_progress_at": None,
           "finished_at": None, "duration_s": None, "result": None, "error": None,
           "timeout_s": 1200, "job_type": "pipeline", "market": None,
           "trigger_source": "manual"}
    row.update(kw)
    return row


# ── 1. DB 失败绝不影响 job 结果 ──────────────────────────────────────────

def test_db_failure_does_not_break_job(tmp_path):
    calls = _counter()
    store = JobStore(store_dir=tmp_path / "jobs")

    jid, _ = store.create()
    store.start(jid)
    store.succeed(jid, {"universe_size": 42})
    _drain()

    # 镜像写全失败了, 但 job 本身毫发无伤
    assert calls, "sink 应该被调用过(失败也是调用)"
    j = store.get(jid)
    assert j["status"] == "succeeded"
    assert j["result"] == {"universe_size": 42}
    # JSON 权威源照常落盘
    path = tmp_path / "jobs" / f"{jid}.json"
    assert path.exists()
    assert json.loads(path.read_text("utf-8"))["status"] == "succeeded"


def test_db_failure_keeps_fail_path(tmp_path):
    _counter()
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create()
    store.start(jid)
    store.fail(jid, "boom")
    _drain()
    assert store.get(jid)["status"] == "failed"


# ── 2. 健康状态显式可见 ──────────────────────────────────────────────────

def test_status_becomes_unavailable_with_reason(tmp_path):
    _counter()
    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create()
    store.succeed(jid, None)
    _drain()

    st = db.db_status()
    assert st["status"] == "unavailable"
    assert "simulated db down" in (st["reason"] or "")
    assert st["since"], "必须有 since —— 不可用的起始时刻是排障的第一手信息"


# ── 3. 熔断 ─────────────────────────────────────────────────────────────

def test_breaker_drops_writes_after_three_failures():
    calls = _counter()

    for i in range(3):
        job_state.mirror(_job_row(f"job{i}"), "succeed")
        _drain()
    assert len(calls) == 3
    assert db.db_status()["breaker_open"] is True

    before_dropped = db.db_status()["dropped_writes"]
    # 第 4 次: 熔断窗口内直接丢弃 —— 不建连、不等待, **连 sink 都不调用**
    assert job_state.mirror(_job_row("job3"), "succeed") is False
    assert len(calls) == 3, "熔断窗口内不得再发起任何 DB 操作"
    assert db.db_status()["dropped_writes"] == before_dropped + 1


def test_breaker_recovers_after_window(monkeypatch):
    monkeypatch.setattr(db, "_BREAKER_WINDOW_S", 0.05)   # 缩短窗口, 免得测试睡 60s
    calls = _counter()
    for i in range(3):
        job_state.mirror(_job_row(f"job{i}"), "succeed")
        _drain()
    assert db.db_status()["breaker_open"] is True
    time.sleep(0.1)                                      # 等窗口过去

    # 修好 sink: 窗口过后的下一次写成功 → 状态复位
    job_state.set_mirror_sink(lambda row: None)
    assert job_state.mirror(_job_row("job9"), "succeed") is True
    _drain()

    st = db.db_status()
    assert st["status"] == "ok"
    assert st["breaker_open"] is False
    assert st["reason"] is None
    assert len(calls) == 3


# ── 4. API 层的显式声明 ─────────────────────────────────────────────────

def test_list_jobs_exposes_history_db_unavailable(monkeypatch, tmp_path):
    from app.api import pipeline as api_pipeline

    # 手动置成"熔断中": 既不会去碰真实实例, 也不会因为写成功把状态复位成 ok
    _simulate_db_down(monkeypatch)
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(api_pipeline, "job_store", store)

    jid, _ = store.create()
    store.start(jid)
    store.succeed(jid, {"universe_size": 1})

    resp = api_pipeline.list_jobs(limit=10)
    # 实时进度照常返回(来自内存/JSON, 与 DB 健康无关)
    assert resp["jobs"], "DB 挂了实时进度也必须照常返回"
    assert resp["history_db"]["status"] == "unavailable"
    assert "simulated db down" in resp["history_db"]["reason"]


def test_history_degrades_to_local_json_with_reason(monkeypatch, tmp_path):
    from app.api import pipeline as api_pipeline
    from app.services import pipeline_jobs

    _simulate_db_down(monkeypatch)
    # fetch 直接报不可用: 保证用例不触网、不依赖真实实例
    monkeypatch.setattr(db, "fetch", lambda fn: (False, None))
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(api_pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)

    jid, _ = store.create()
    store.start(jid)
    store.succeed(jid, {"universe_size": 1})

    resp = api_pipeline.job_history(limit=10)
    assert resp["available"] is False
    assert resp["source"] == "local_json"
    assert "simulated db down" in resp["reason"]
    assert resp["items"], "降级也必须给数据 —— 绝不返回无解释的空列表"
    assert resp["items"][0]["id"] == jid


def test_stats_declares_unavailable_without_fake_aggregates(monkeypatch):
    from app.api import pipeline as api_pipeline

    monkeypatch.setattr(db, "_status", "unavailable")
    monkeypatch.setattr(db, "_reason", "simulated db down")
    monkeypatch.setattr(db, "fetch", lambda fn: (False, None))

    resp = api_pipeline.job_stats(days=30)
    assert resp["available"] is False
    assert resp["source"] == "none"
    assert resp["stats"] is None          # 绝不用本地 50 条冒充 30 天聚合
    assert "simulated db down" in resp["reason"]


# ── 5. 开关关闭: 零 DB 调用 ─────────────────────────────────────────────

def test_disabled_means_zero_db_calls(tmp_path):
    calls = _counter()

    # conftest 已把 TSP_POSTGRES_ENABLED 置 false
    assert settings.tsp_postgres_enabled is False
    assert db.init() is False
    assert db.db_status()["status"] == "disabled"

    store = JobStore(store_dir=tmp_path / "jobs")
    jid, _ = store.create()
    store.start(jid)
    store.progress(jid, "sync", 10, "x")
    store.succeed(jid, {"universe_size": 1})
    _drain()

    assert calls == [], "开关关闭时整个进程不得发起任何 DB 调用"
    assert store.get(jid)["status"] == "succeeded"

    # 关闭态下 /history 直接回退本地 JSON, 并显式说明原因
    payload = job_state.history_payload(limit=10)
    assert payload["available"] is False
    assert payload["source"] == "local_json"
    assert "TSP_POSTGRES_ENABLED" in payload["reason"]
