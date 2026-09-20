"""热点同步 job 测试 (调度注册 + 手动执行, 不打网络)。"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.jobs.hotspot_sync import (
    HOTSPOT_SYNC_JOB_ID,
    HOTSPOT_SYNC_JOB_ID_HK,
    HOTSPOT_SYNC_JOB_ID_US,
    HOTSPOT_SYNC_JOB_ID_US_LATE,
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
    """四个 job: cn/hk 09:05-15:35 (mon-fri), us 21:05-23:35 (mon-fri) + 00:05-03:35 (tue-sat)。"""
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    assert len(scheduler.jobs) == 4
    jobs = {job["id"]: job for job in scheduler.jobs}
    assert list(jobs) == [
        HOTSPOT_SYNC_JOB_ID,
        HOTSPOT_SYNC_JOB_ID_HK,
        HOTSPOT_SYNC_JOB_ID_US,
        HOTSPOT_SYNC_JOB_ID_US_LATE,
    ]
    for job in jobs.values():
        assert isinstance(job["trigger"], CronTrigger)
        assert job["misfire_grace_time"] == 600
        assert job["replace_existing"] is True
        assert str(job["trigger"].timezone) == "Asia/Shanghai"
        assert callable(job["func"])

    def _expr(job_id, name):
        return str(next(f for f in jobs[job_id]["trigger"].fields if f.name == name))

    for job_id in jobs:
        assert _expr(job_id, "minute") == "5,35"

    assert (_expr(HOTSPOT_SYNC_JOB_ID, "hour"), _expr(HOTSPOT_SYNC_JOB_ID, "day_of_week")) == ("9-15", "mon-fri")
    assert (_expr(HOTSPOT_SYNC_JOB_ID_HK, "hour"), _expr(HOTSPOT_SYNC_JOB_ID_HK, "day_of_week")) == ("9-15", "mon-fri")
    assert (_expr(HOTSPOT_SYNC_JOB_ID_US, "hour"), _expr(HOTSPOT_SYNC_JOB_ID_US, "day_of_week")) == ("21-23", "mon-fri")
    # 夜盘后半段: 0-3 必须配 tue-sat, 否则周五夜盘断档 / 周一凌晨空转
    assert (
        _expr(HOTSPOT_SYNC_JOB_ID_US_LATE, "hour"),
        _expr(HOTSPOT_SYNC_JOB_ID_US_LATE, "day_of_week"),
    ) == ("0-3", "tue-sat")


def test_us_late_job_covers_friday_overnight_session(data_dir):
    """核心回归: 周五夜盘后半段 (北京时间周六 00:05~03:35) 必须被覆盖。

    day_of_week 是日历日口径, 跨日行情的 0-3 段配 tue-sat 才能覆盖到周五夜盘,
    同时跳过周日凌晨与周一凌晨 (美股休市) 的空转。
    """
    tz = ZoneInfo("Asia/Shanghai")
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    jobs = {job["id"]: job for job in scheduler.jobs}
    trigger = jobs[HOTSPOT_SYNC_JOB_ID_US_LATE]["trigger"]

    def _next(after: datetime) -> datetime:
        return trigger.get_next_fire_time(None, after + timedelta(seconds=1)).astimezone(tz)

    friday_2335 = datetime(2026, 9, 25, 23, 35, tzinfo=tz)  # 周五 23:35 (us job 最后一轮)
    assert _next(friday_2335) == datetime(2026, 9, 26, 0, 5, tzinfo=tz)  # 周六 00:05

    fires: list[str] = []
    cursor = friday_2335
    for _ in range(8):
        cursor = _next(cursor)
        fires.append(cursor.strftime("%a %H:%M"))
    assert fires == [
        "Sat 00:05", "Sat 00:35", "Sat 01:05", "Sat 01:35",
        "Sat 02:05", "Sat 02:35", "Sat 03:05", "Sat 03:35",
    ]
    # 周日凌晨不再空转: 下一次是下周二 00:05
    assert _next(datetime(2026, 9, 26, 3, 35, tzinfo=tz)) == datetime(2026, 9, 29, 0, 5, tzinfo=tz)


def test_us_evening_job_does_not_fire_on_weekend(data_dir):
    """21-23 段只在周一~周五: 周五 23:35 之后下一次是下周一 21:05。"""
    tz = ZoneInfo("Asia/Shanghai")
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    jobs = {job["id"]: job for job in scheduler.jobs}
    trigger = jobs[HOTSPOT_SYNC_JOB_ID_US]["trigger"]

    def _next(after: datetime) -> datetime:
        return trigger.get_next_fire_time(None, after + timedelta(seconds=1)).astimezone(tz)

    assert _next(datetime(2026, 9, 25, 23, 35, tzinfo=tz)) == datetime(2026, 9, 28, 21, 5, tzinfo=tz)


def test_registered_jobs_run_sync_for_their_own_market(data_dir, monkeypatch):
    """每个 job 的 lambda 必须带上自己那个 market, 不能三个都跑 cn。"""
    from app.jobs import hotspot_sync as job_module

    calls: list[tuple] = []
    monkeypatch.setattr(
        job_module,
        "run_hotspot_sync",
        lambda dd, **kw: calls.append((dd, kw.get("market"))) or {"status": "ok", "rows": 0},
    )
    scheduler = _FakeScheduler()
    register_hotspot_jobs(scheduler, data_dir)
    for job in scheduler.jobs:
        job["func"]()
    # 美股两个 job 都跑 us, 且顺序固定 cn / hk / us / us_late
    assert calls == [(data_dir, "cn"), (data_dir, "hk"), (data_dir, "us"), (data_dir, "us")]


def test_run_sync_default_source_comes_from_select_source(data_dir, monkeypatch):
    """回归: 默认源走 select_source (A 股 = 本地同花顺概念源), 不再硬编码 akshare。

    akshare 东财源依赖 push2.eastmoney.com, 代理环境不可达 (实测 28.7s 超时后空
    列表), 每半小时用它刷一次会把 job_state.last_status 刷成 empty。
    """
    import app.services.hotspot.service as service_module
    from app.services.hotspot.source import select_source

    seen: list[str] = []
    original = service_module.select_source
    monkeypatch.setattr(
        service_module,
        "select_source",
        lambda market, **kw: seen.append(market) or original(market, **kw),
    )
    run_hotspot_sync(data_dir)  # 不注入 source → 必须走 select_source 默认值
    assert seen == ["cn"]
    # 默认值本身也不是 akshare 源
    assert type(select_source("cn")).__name__ == "CnConceptHotspotSource"


def test_cli_once_succeeds_with_default_source(tmp_path, monkeypatch, capsys):
    """CLI --once 成功路径: 默认源走 select_source, 这里注入 fake 源, 不含网络请求。"""
    import app.services.hotspot.service as service_module
    from app.jobs.hotspot_sync import _cli
    from app.services.hotspot.akshare_source import AkshareHotspotSource

    class _FakeAk:
        def stock_board_concept_name_em(self):
            return _FakeDF([{"排名": 1, "板块名称": "人工智能", "涨跌幅": 8.26, "领涨股票": "景嘉微"}])

        def stock_board_industry_name_em(self):
            return _FakeDF([])

    fake_source = AkshareHotspotSource(ak=_FakeAk())
    monkeypatch.setattr(service_module, "select_source", lambda market, **kw: fake_source)

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
