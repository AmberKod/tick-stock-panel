"""A 股盘后管道启动 catch-up 的判定与触发守卫。"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.jobs import daily_pipeline


@pytest.fixture
def cn_repo(tmp_path, monkeypatch):
    """构造只有 date= 分区日 K 的假仓储, 并把真实管道调用替换为计数器。"""
    calls: list[str] = []

    class _Store:
        data_dir = tmp_path

    class _Repo:
        store = _Store()
        db = None

        def refresh_cache(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        daily_pipeline,
        "run_pipeline_then_refresh",
        lambda *args, **kwargs: calls.append("pipeline") or {"ok": True},
    )
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: None)
    monkeypatch.setattr(daily_pipeline, "run_now", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(daily_pipeline, "_scheduled_pipeline_task", lambda fn: fn() or calls.append("task"))
    return _Repo(), calls


def _write_day(root: Path, day: date) -> None:
    directory = root / "kline_daily" / f"date={day.isoformat()}"
    directory.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600000.SH"], "date": [day], "close": [10.0]}).write_parquet(directory / "part.parquet")


def test_catchup_runs_when_pipeline_window_missed(tmp_path, cn_repo):
    repo, calls = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 11))  # 周五
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 15, 16, 30))
    assert result["status"] == "ok"
    assert "pipeline" in calls


def test_catchup_skipped_before_window(tmp_path, cn_repo):
    repo, calls = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 11))
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 15, 15, 45))
    assert result["status"] == "skipped"
    assert not calls


def test_catchup_skipped_on_weekend(tmp_path, cn_repo):
    repo, calls = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 11))
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 19, 18, 0))  # 周六
    assert result["status"] == "skipped"
    assert not calls


def test_catchup_skipped_when_data_fresh(tmp_path, cn_repo):
    repo, calls = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 15))
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 15, 18, 0))
    assert result["status"] == "skipped"
    assert not calls


def test_catchup_tolerates_weekend_gap(tmp_path, cn_repo):
    repo, calls = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 11))  # 周五
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 14, 18, 0))  # 周一, 差 3 天
    assert result["status"] == "skipped"
    assert not calls


def test_catchup_runs_without_any_daily_data(tmp_path, cn_repo):
    repo, calls = cn_repo
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 15, 18, 0))
    assert result["status"] == "ok"
    assert "pipeline" in calls


def test_catchup_weekend_without_data_warns_instead_of_ok(tmp_path, cn_repo, caplog):
    """周末 + 完全无日 K: 不补跑, 但必须告警 (不得写成"无需补跑")。

    与港美同源的伪装风险: _cn_catchup_needed 对无数据是 return True
    (fail-closed, 交给管道建基线), 周末分支却先于数据检查返回 False,
    于是"压根没数据"也会打印"A 股日 K 无需补跑"。这里锁定告警口径。
    """
    import logging

    repo, calls = cn_repo
    with caplog.at_level(logging.INFO, logger="app.jobs.daily_pipeline"):
        result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 19, 18, 0))  # 周六
    assert result["status"] == "skipped"
    assert result["reason"] == "no_data"
    assert not calls
    assert "无需补跑" not in caplog.text
    assert "新鲜度不可判定" in caplog.text


def test_catchup_failure_is_silent(tmp_path, cn_repo, monkeypatch):
    repo, _ = cn_repo
    _write_day(repo.store.data_dir, date(2026, 9, 11))

    def boom(*args, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(daily_pipeline, "run_pipeline_then_refresh", boom)
    result = daily_pipeline.run_daily_pipeline_catchup(repo, None, now=datetime(2026, 9, 15, 18, 0))
    assert result["status"] == "failed"
