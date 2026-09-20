"""强度梯队接入 daily_pipeline 自动补算 (P1-B 收口)。

验证两件事:
1. services/strength_ladder.compute_strength_ladder_incremental — 缺口检测 + 回溯上限
2. jobs/daily_pipeline._compute_strength_ladder_step — 市场路由 + 软失败隔离

设计要点(与 regime 调度保持一致):
- 市场启用判定: instruments/{hk,us}_instruments.parquet 存在才启用
- 单市场失败: 软失败, 记 stage_errors + skipped, 不影响其他市场
- A 股(cn): 不启用 — A 股走连板梯队, 梯队服务本身也拒绝 cn
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.jobs import daily_pipeline as dp
from app.services import strength_ladder


class _FakeStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir


class _FakeRepo:
    def __init__(self, data_dir: Path) -> None:
        self.store = _FakeStore(data_dir)


def _mk_instruments(data_dir: Path, market: str) -> None:
    p = data_dir / "instruments"
    p.mkdir(parents=True, exist_ok=True)
    (p / f"{market}_instruments.parquet").write_bytes(b"fake")


# ────────────────────── 日期归一化(真实踩坑) ──────────────────────

def test_as_date_normalizes_datetime():
    """datetime 必须被降成 date。

    坑: datetime 是 date 的子类, `isinstance(dt, date)` 恒为 True, 所以
    "isinstance(d, date) 就用原值" 的写法会让港美 enriched 的 Datetime('us')
    原样溜过去, 后续与 date.today() 比较直接 TypeError。
    """
    from datetime import datetime

    from app.services.regime_builder import _as_date

    assert _as_date(datetime(2026, 9, 10, 0, 0)) == date(2026, 9, 10)
    assert _as_date(date(2026, 9, 10)) == date(2026, 9, 10)
    assert _as_date("2026-09-10") == date(2026, 9, 10)
    assert _as_date("2026-09-10 00:00:00") == date(2026, 9, 10)
    # 关键回归: 归一化后能与 date 直接比较(原先会 TypeError)
    assert _as_date(datetime(2026, 9, 10, 0, 0)) <= date(2026, 9, 14)


def test_incremental_survives_datetime_dates(monkeypatch, tmp_path):
    """enriched 返回 Datetime(港美真实情况) 时不炸, 且能正确补算。"""
    from datetime import datetime

    d1 = datetime(2026, 9, 8, 0, 0)   # enriched 给的是 datetime
    d2 = datetime(2026, 9, 9, 0, 0)

    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set",
        lambda repo, market="cn": {d1, d2},
    )
    monkeypatch.setattr(
        strength_ladder, "load_strength_ladder_history", lambda *a, **k: pl.DataFrame(),
    )
    seen = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_for_day",
        lambda data_dir, day, market="hk": seen.append(day) or pl.DataFrame({
            "date": [day], "market": [market], "band": ["m15"],
            "symbol": ["W.HK"], "momentum_20d": [0.18],
        }),
    )
    monkeypatch.setattr(strength_ladder, "upsert_strength_ladder", lambda *a, **k: None)

    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, today=date(2026, 9, 10), market="hk",
    )
    assert out == 2
    assert all(isinstance(d, date) and not isinstance(d, datetime) for d in seen)


# ────────────────────── 服务层: 增量补算 ──────────────────────

def test_incremental_rejects_cn(monkeypatch, tmp_path):
    """cn 不支持(走 A 股连板梯队), 直接返回 0 且不碰文件系统。"""
    called = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_for_day",
        lambda *a, **k: called.append(1) or pl.DataFrame(),
    )
    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, market="cn",
    )
    assert out == 0
    assert not called


def test_incremental_no_gap_returns_zero(monkeypatch, tmp_path):
    """enriched 日期都已落盘 → 无缺口, 返回 0。"""
    d = date(2026, 9, 10)

    def _fake_dates(repo, market="cn"):
        return {d}

    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set", _fake_dates,
    )
    # 已有 ladder 含该日
    hist = pl.DataFrame({
        "date": [d], "market": ["hk"], "band": ["m25"],
        "symbol": ["00700.HK"], "momentum_20d": [0.30],
    })
    monkeypatch.setattr(
        strength_ladder, "load_strength_ladder_history", lambda *a, **k: hist,
    )
    called = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_for_day",
        lambda *a, **k: called.append(1) or pl.DataFrame(),
    )
    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, today=d, market="hk",
    )
    assert out == 0
    assert not called


def test_incremental_fills_gap(monkeypatch, tmp_path):
    """enriched 有 3 天、ladder 只有 1 天 → 补 2 天。"""
    d1, d2, d3 = date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)

    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set",
        lambda repo, market="cn": {d1, d2, d3},
    )
    monkeypatch.setattr(
        strength_ladder, "load_strength_ladder_history", lambda *a, **k: pl.DataFrame(),
    )

    seen: list[date] = []

    def _fake_day(data_dir, day, market="hk"):
        seen.append(day)
        return pl.DataFrame({
            "date": [day], "market": [market], "band": ["m25"],
            "symbol": ["X.HK"], "momentum_20d": [0.30],
        })

    monkeypatch.setattr(strength_ladder, "compute_strength_ladder_for_day", _fake_day)
    monkeypatch.setattr(strength_ladder, "upsert_strength_ladder", lambda *a, **k: None)

    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, today=d3, market="hk",
    )
    assert seen == [d1, d2, d3]
    assert out == 3


def test_incremental_respects_backfill_limit(monkeypatch, tmp_path):
    """缺口 5 天但 max_backfill_days=2 → 只补最近 2 天(避免首次跑拖垮日管道)。"""
    days = [date(2026, 9, d) for d in (1, 2, 3, 4, 5)]

    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set",
        lambda repo, market="cn": set(days),
    )
    monkeypatch.setattr(
        strength_ladder, "load_strength_ladder_history", lambda *a, **k: pl.DataFrame(),
    )
    seen: list[date] = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_for_day",
        lambda data_dir, day, market="hk": seen.append(day) or pl.DataFrame({
            "date": [day], "market": [market], "band": ["m8"],
            "symbol": ["Y.HK"], "momentum_20d": [0.10],
        }),
    )
    monkeypatch.setattr(strength_ladder, "upsert_strength_ladder", lambda *a, **k: None)

    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, today=days[-1], market="hk",
        max_backfill_days=2,
    )
    assert seen == [days[-2], days[-1]]  # 09-04, 09-05
    assert out == 2


def test_incremental_soft_fails_single_day(monkeypatch, tmp_path):
    """单日计算抛异常 → 跳过该日, 其他日照常补(不阻断)。"""
    d1, d2 = date(2026, 9, 8), date(2026, 9, 9)
    monkeypatch.setattr(
        "app.services.regime_builder.enriched_date_set",
        lambda repo, market="cn": {d1, d2},
    )
    monkeypatch.setattr(
        strength_ladder, "load_strength_ladder_history", lambda *a, **k: pl.DataFrame(),
    )

    def _flakey(data_dir, day, market="hk"):
        if day == d1:
            raise RuntimeError("boom")
        return pl.DataFrame({
            "date": [day], "market": [market], "band": ["m3"],
            "symbol": ["Z.HK"], "momentum_20d": [0.05],
        })

    monkeypatch.setattr(strength_ladder, "compute_strength_ladder_for_day", _flakey)
    monkeypatch.setattr(strength_ladder, "upsert_strength_ladder", lambda *a, **k: None)

    out = strength_ladder.compute_strength_ladder_incremental(
        _FakeRepo(tmp_path), tmp_path, today=d2, market="hk",
    )
    assert out == 1  # 只有 d2 成功


# ────────────────────── 调度层: 市场路由与软失败 ──────────────────────

def test_step_skips_when_no_universe(tmp_path):
    """港美 universe 都没同步 → 跳过, 不计入 stage_errors(不是错误)。"""
    skipped: list = []
    errors: list = []
    out = dp._compute_strength_ladder_step(
        repo=_FakeRepo(tmp_path), emit=lambda *a, **k: None,
        skipped=skipped, stage_errors=errors,
    )
    assert out == 0
    assert "strength_ladder" in skipped
    assert not errors


def test_step_only_enabled_markets(tmp_path, monkeypatch):
    """只建了 hk universe → 只补 hk, 不碰 us。"""
    _mk_instruments(tmp_path, "hk")
    seen: list[str] = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_incremental",
        lambda repo, data_dir, market="hk", **kw: seen.append(market) or 7,
    )
    out = dp._compute_strength_ladder_step(
        repo=_FakeRepo(tmp_path), emit=lambda *a, **k: None,
        skipped=[], stage_errors=[],
    )
    assert seen == ["hk"]
    assert out == 7


def test_step_both_markets(tmp_path, monkeypatch):
    """hk + us universe 都在 → 两个市场都补, 行数累加。"""
    _mk_instruments(tmp_path, "hk")
    _mk_instruments(tmp_path, "us")
    seen: list[str] = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_incremental",
        lambda repo, data_dir, market="hk", **kw: seen.append(market) or 5,
    )
    out = dp._compute_strength_ladder_step(
        repo=_FakeRepo(tmp_path), emit=lambda *a, **k: None,
        skipped=[], stage_errors=[],
    )
    assert seen == ["hk", "us"]
    assert out == 10


def test_step_soft_failure_isolated(tmp_path, monkeypatch):
    """hk 抛异常 → 记录后继续跑 us, 不阻断。"""
    _mk_instruments(tmp_path, "hk")
    _mk_instruments(tmp_path, "us")

    def _flakey(repo, data_dir, market="hk", **kw):
        if market == "hk":
            raise RuntimeError("hk boom")
        return 4

    monkeypatch.setattr(strength_ladder, "compute_strength_ladder_incremental", _flakey)
    skipped: list = []
    errors: list = []
    out = dp._compute_strength_ladder_step(
        repo=_FakeRepo(tmp_path), emit=lambda *a, **k: None,
        skipped=skipped, stage_errors=errors,
    )
    assert out == 4  # us 成功
    assert any("strength_ladder[hk]" in s for s in skipped)
    assert any("hk boom" in e for e in errors)


@pytest.mark.parametrize("market", ["cn"])
def test_step_never_enables_cn(tmp_path, market, monkeypatch):
    """A 股永远不进梯队调度(连板梯队由 market_phase 等协同)。"""
    _mk_instruments(tmp_path, "cn")  # 即便误建了 cn instruments 也不启用
    seen: list[str] = []
    monkeypatch.setattr(
        strength_ladder, "compute_strength_ladder_incremental",
        lambda repo, data_dir, market="hk", **kw: seen.append(market) or 1,
    )
    dp._compute_strength_ladder_step(
        repo=_FakeRepo(tmp_path), emit=lambda *a, **k: None,
        skipped=[], stage_errors=[],
    )
    assert seen == []
