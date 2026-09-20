"""热点同步 job 测试 (调度注册 + 手动执行, 不打网络)。"""
from __future__ import annotations

from pathlib import Path

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.jobs.hotspot_sync import (
    HOTSPOT_SYNC_JOB_ID,
    register_hotspot_jobs,
    run_hotspot_sync,
)
from app.services.hotspot.source import StubHotspotSource
from app.services.hotspot.storage import HotspotStorage


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def add_job(self, func, trigger=None, **kwargs):
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})
        return None


class _FakeDF:
    """pandas DataFrame 最小仿真。"""

    def __init__(self, rows):
        self._rows = rows

    @property
    def empty(self):
        return not self._rows

    def to_dict(self, orient):
        return list(self._rows)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def test_run_sync_with_stub_source_persists_snapshot(data_dir):
    result = run_hotspot_sync(data_dir, source=StubHotspotSource())
    assert result["status"] == "ok"
    assert result["rows"] > 0
    state = HotspotStorage(data_dir).read_job_state()
    assert state["last_status"] == "success"


def test_run_sync_skips_unsupported_market(data_dir):
    """注入不支持该市场的 stub 源 → skipped (港美默认源已可用, 需显式注入)。"""
    result = run_hotspot_sync(data_dir, market="us", source=StubHotspotSource())
    assert result["status"] == "skipped"
    assert result["rows"] == 0
    assert HotspotStorage(data_dir).read_job_state()["last_status"] == "skipped"


def test_run_sync_with_fake_ak_share_source(data_dir):
    """注入 fake akshare 源: 走真实 akshare source 代码路径但无网络。"""
    from app.services.hotspot.akshare_source import AkshareHotspotSource

    class _FakeAk:
        def stock_board_concept_name_em(self):
            return _FakeDF([{"排名": 1, "板块名称": "人工智能", "涨跌幅": 8.26}])

        def stock_board_industry_name_em(self):
            return _FakeDF([])

    result = run_hotspot_sync(data_dir, source=AkshareHotspotSource(ak=_FakeAk()))
    assert result["status"] in {"ok", "degraded"}
    assert result["rows"] == 1
    topics = HotspotStorage(data_dir).read_topics()
    assert topics[0].topic == "人工智能"
    assert topics[0].change_pct == pytest.approx(0.0826)


def test_register_hotspot_jobs_uses_cron_trigger(data_dir):
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    assert len(scheduler.jobs) == 1
    job = scheduler.jobs[0]
    assert job["id"] == HOTSPOT_SYNC_JOB_ID
    assert isinstance(job["trigger"], CronTrigger)
    assert job["misfire_grace_time"] == 600
    assert job["replace_existing"] is True
    # 调度触发执行时不抛异常 (stub 依赖默认源, 此处只验证可调用)
    assert callable(job["func"])


def test_cli_once_succeeds_with_injected_akshare(tmp_path, monkeypatch, capsys):
    """CLI --once 成功路径: monkeypatch akshare 接口, 不含网络请求。"""
    ak = pytest.importorskip("akshare")
    from app.jobs.hotspot_sync import _cli

    rows = [{"排名": 1, "板块名称": "人工智能", "涨跌幅": 8.26, "领涨股票": "景嘉微"}]
    monkeypatch.setattr(ak, "stock_board_concept_name_em", lambda: _FakeDF(rows), raising=False)
    monkeypatch.setattr(ak, "stock_board_industry_name_em", lambda: _FakeDF([]), raising=False)

    target = tmp_path / "cli_data"
    monkeypatch.setattr(
        "sys.argv",
        ["hotspot_sync", "--once", "--data-dir", str(target)],
    )
    assert _cli() == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out or '"status": "degraded"' in out
    stored = HotspotStorage(target).read_topics()
    assert [t.topic for t in stored] == ["人工智能"]


def test_registered_job_callable_runs_sync(data_dir, monkeypatch):
    """注册的 lambda 应调用当前模块的 run_hotspot_sync (可 monkeypatch, 不打网络)。"""
    from app.jobs import hotspot_sync as job_module

    calls: list = []
    monkeypatch.setattr(
        job_module,
        "run_hotspot_sync",
        lambda dd, **kw: calls.append(dd) or {"status": "ok", "rows": 0},
    )
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    scheduler.jobs[0]["func"]()
    assert calls == [data_dir]
