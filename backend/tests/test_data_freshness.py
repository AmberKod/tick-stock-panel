"""数据新鲜度画像单测。

覆盖: 各分区布局 (A股 per-date / 港美 per-symbol) 的日期提取、
五种状态判定、缺口区间不倒挂。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.services import data_freshness

TODAY = date(2026, 9, 18)


def _write(path: Path, rows: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "date": [date.fromisoformat(r[1]) for r in rows],
            "close": [1.0] * len(rows),
        }
    ).write_parquet(path)


# 撑起历史深度的基线日期: 不写它的话每个用例都只有几天历史,
# 会被统一判成 shallow, 掩盖真正要测的状态。
_OLD = "2025-01-02"


def _cn(root: Path, table: str, days: list[str], *, history: bool = True) -> None:
    for d in ([_OLD] if history else []) + days:
        _write(root / table / f"date={d}" / "part.parquet", [("000001.SZ", d)])


def _sym(root: Path, table: str, symbol: str, days: list[str], *, history: bool = True) -> None:
    rows = ([_OLD] if history else []) + days
    _write(root / table / f"symbol={symbol}" / "part.parquet",
           [(symbol, d) for d in rows])


# ── A 股 (per-date 分区) ───────────────────────────────────────


def test_cn_ok_when_within_tolerance(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-15", "2026-09-16", "2026-09-17"])
    _cn(tmp_path, "kline_daily", ["2026-09-16", "2026-09-17"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "ok"
    assert r["latest_date"] == "2026-09-17"
    assert r["earliest_date"] == _OLD
    assert r["stale_days"] == 1
    assert r["gap"] is None
    assert r["coverage_unit_label"] == "交易日"


def test_cn_stale_beyond_tolerance(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-08", "2026-09-10"])
    _cn(tmp_path, "kline_daily", ["2026-09-10"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "stale"
    assert r["gap"]["from"] == "2026-09-11"
    assert r["gap"]["to"] == "2026-09-18"
    assert r["gap"]["missing_days"] == 8


def test_cn_behind_raw_when_enriched_lags(tmp_path):
    """原始数据已到位但 enriched 没算 → 不是'没拉到', 是'没算'。"""
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-15"])
    _cn(tmp_path, "kline_daily", ["2026-09-16", "2026-09-17"])

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "behind_raw"
    assert r["gap"]["from"] == "2026-09-16"
    assert r["gap"]["to"] == "2026-09-17"


def test_empty_market_has_no_gap(tmp_path):
    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "empty"
    assert r["latest_date"] is None
    assert r["gap"] is None  # 全新部署不编造区间


# ── 港美股 (per-symbol 分区, 抽样) ──────────────────────────────


def test_hk_modal_date_and_coverage(tmp_path):
    # 8 只标的在最新日, 2 只落后一天 → 众数 = 09-18, 覆盖率 0.8
    for i in range(8):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK",
             ["2026-09-16", "2026-09-17", "2026-09-18"])
    for i in range(8, 10):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK",
             ["2026-09-16", "2026-09-17"])
    for i in range(10):
        _sym(tmp_path, "kline_daily", f"{i:05d}.HK", ["2026-09-18"])

    r = data_freshness.market_freshness(tmp_path, "HK", today=TODAY, sample=10)
    assert r["status"] == "ok"
    assert r["latest_date"] == "2026-09-18"
    assert r["coverage_ratio"] == pytest.approx(0.8)
    assert r["coverage_units"] == 10
    assert r["coverage_unit_label"] == "标的"


def test_hk_partial_when_symbols_scattered(tmp_path):
    """各标的停在不同日期 → 众数占比低, 判定为部分同步。"""
    targets = ["2026-09-18", "2026-09-17", "2026-09-16", "2026-09-15", "2026-09-14"]
    for i in range(10):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.HK", [targets[i % 5]])
        _sym(tmp_path, "kline_daily", f"{i:05d}.HK", ["2026-09-18"])

    r = data_freshness.market_freshness(tmp_path, "HK", today=TODAY, sample=10)
    assert r["status"] == "partial"
    assert r["coverage_ratio"] <= 0.5
    # 日期已到最新, 缺口区间退化成"当天", 绝不出现 from > to
    assert r["gap"]["from"] == r["gap"]["to"]
    assert r["gap"]["missing_days"] == 1


def test_us_stale_gap(tmp_path):
    for i in range(5):
        _sym(tmp_path, "kline_hk_us_enriched", f"{i:05d}.US", ["2026-09-10", "2026-09-11"])
        _sym(tmp_path, "kline_daily", f"{i:05d}.US", ["2026-09-11"])

    r = data_freshness.market_freshness(tmp_path, "US", today=TODAY, sample=5)
    assert r["status"] == "stale"
    assert r["gap"]["from"] == "2026-09-12"
    assert r["gap"]["to"] == "2026-09-18"


# ── 历史深度不足 ───────────────────────────────────────────────


def test_history_insufficient_suggests_longer_range(tmp_path):
    """只有 10 天历史 → 建议补一整年, 而不是只补尾部几天。"""
    recent = [f"2026-09-{d:02d}" for d in range(8, 18)]
    _cn(tmp_path, "kline_daily_enriched", recent, history=False)
    _cn(tmp_path, "kline_daily", recent, history=False)

    r = data_freshness.market_freshness(tmp_path, "CN", today=TODAY)
    assert r["status"] == "shallow"
    assert r["history_insufficient"] is True
    assert r["gap"]["from"] == "2025-09-18"   # today - 365
    assert r["gap"]["to"] == "2026-09-18"


# ── 聚合层 ────────────────────────────────────────────────────


def test_get_data_freshness_marks_sampled_cache(tmp_path):
    _cn(tmp_path, "kline_daily_enriched", ["2026-09-17"])

    first = data_freshness.get_data_freshness(tmp_path, ("CN",), active_job={"id": "j1"})
    assert first["cached"] is False
    assert first["active_job"] == {"id": "j1"}

    second = data_freshness.get_data_freshness(tmp_path, ("CN",), active_job=None)
    assert second["cached"] is True
    assert second["active_job"] is None  # 活跃任务实时注入, 不走缓存

    data_freshness.invalidate_cache()
    assert data_freshness.get_data_freshness(tmp_path, ("CN",)) ["cached"] is False


def test_unknown_market_degrades_gracefully(tmp_path):
    payload = data_freshness.get_data_freshness(tmp_path, ("CN", "HK", "US"))
    assert len(payload["markets"]) == 3
    assert {m["market"] for m in payload["markets"]} == {"CN", "HK", "US"}


# ── API 层: 活跃任务市场推断 ────────────────────────────────────


def test_job_market_inference():
    from app.api.data import _job_market

    assert _job_market({"result": {"market": "HK"}}, None) == "HK"
    assert _job_market({}, "HK 日K同步 1629/2816") == "HK"
    assert _job_market({}, "美股日K同步 3/6071") == "US"
    assert _job_market({}, "A股盘后管道") == "CN"
    assert _job_market({}, "无市场信息") is None
