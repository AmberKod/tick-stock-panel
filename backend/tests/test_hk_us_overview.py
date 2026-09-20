"""港美 overview builder (阶段 B-2) 单测。

覆盖: 结构完整性 (对齐 A股 overview schema)、空数据分支、聚合正确性。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl

from app.services.hk_us_overview_builder import build_hk_us_overview

# 对齐 A股 /api/overview/market 的关键字段
_OVERVIEW_KEYS = [
    "as_of", "market", "quote_status", "indices",
    "breadth", "amount", "boards", "limit", "distribution", "trend",
    "activity", "radar", "emotion",
    "top_gainers", "top_losers", "turnover_leaders", "active_leaders",
    "concept_rank", "industry_rank",
]


def _write_fake_enriched(tmp_path, market: str, rows: list[dict]) -> None:
    """在 tmp_path 造一个 kline_hk_us_enriched/symbol=* 分区。"""
    suffix = f".{market.upper()}"
    for r in rows:
        sym = r["symbol"] + suffix
        df = pl.DataFrame({
            "symbol": [sym],
            "date": [datetime(2026, 9, 3)],
            "open": [10.0], "high": [12.0], "low": [9.0],
            "close": [r["close"]], "volume": [1e6], "amount": [r.get("amount", 1e7)],
            "prev_close": [10.0],
            "ma5": [r["close"] - 0.1], "ma20": [r["close"] - 0.5], "ma60": [r["close"] - 1.0],
            "high_60d": [r["close"] + 5.0], "low_60d": [r["close"] - 5.0],
            "vol_ratio_5d": [1.2], "annual_vol_20d": [0.25],
            "change_pct": [r["change_pct"]],
            "change_amount": [r["change_pct"] * 10.0], "amplitude": [0.03],
            "signal_n_day_high": [r.get("new_high", False)],
            "signal_n_day_low": [r.get("new_low", False)],
            "signal_volume_surge": [False],
        })
        out = tmp_path / "kline_hk_us_enriched" / f"symbol={sym}" / "part.parquet"
        out.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(out)


def _write_fake_instruments(tmp_path, market: str, symbols: list[str]) -> None:
    suffix = f".{market.upper()}"
    df = pl.DataFrame({
        "symbol": [s + suffix for s in symbols],
        "name": [f"名称{s}" for s in symbols],
    })
    out = tmp_path / "instruments" / f"{market.lower()}_instruments.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)


def test_empty_returns_full_structure(tmp_path):
    """无 enriched 数据时仍返回完整结构 (对齐 A股 schema)。"""
    ov = build_hk_us_overview("HK", tmp_path)
    for key in _OVERVIEW_KEYS:
        assert key in ov, f"缺少字段 {key}"
    assert ov["breadth"]["total"] == 0
    assert ov["as_of"] is None


def test_hk_aggregation(tmp_path):
    """港股聚合正确: 广度/强势梯队/四榜/名称 JOIN。"""
    rows = [
        {"symbol": "00700", "close": 100.0, "change_pct": 0.06, "amount": 5e8, "new_high": True},
        {"symbol": "09988", "close": 80.0, "change_pct": -0.03, "amount": 3e8},
        {"symbol": "00001", "close": 70.0, "change_pct": 0.01, "amount": 2e8},
    ]
    _write_fake_enriched(tmp_path, "HK", rows)
    _write_fake_instruments(tmp_path, "HK", ["00700", "09988", "00001"])

    ov = build_hk_us_overview("HK", tmp_path)
    assert ov["breadth"]["total"] == 3
    assert ov["breadth"]["up"] == 2
    assert ov["breadth"]["down"] == 1
    # 强势股 (涨幅>=5%): 只有 00700 (+6%)
    assert ov["limit"]["limit_up"] == 1
    # 四榜首位是涨幅最高的 00700, 名称 JOIN 成功
    assert ov["top_gainers"][0]["symbol"] == "00700.HK"
    assert ov["top_gainers"][0]["name"] == "名称00700"
    # board 映射 (HK 主板)
    assert all(b["board"] == "主板" for b in ov["boards"])
    # 情绪标签存在
    assert ov["emotion"]["label"] in {"强势", "偏暖", "震荡", "偏冷", "冰点"}


def test_hk_gem_board(tmp_path):
    """港股 08 前缀映射创业板。"""
    rows = [
        {"symbol": "08081", "close": 5.0, "change_pct": 0.02, "amount": 1e7},
    ]
    _write_fake_enriched(tmp_path, "HK", rows)
    ov = build_hk_us_overview("HK", tmp_path)
    assert ov["boards"][0]["board"] == "创业板"


def test_us_empty_branch(tmp_path):
    """美股无数据时返回完整结构 + 空 breadth。"""
    ov = build_hk_us_overview("US", tmp_path)
    assert ov["market"] == "US"
    assert ov["breadth"]["total"] == 0
    assert all(r["symbol"].endswith(".US") for r in ov["indices"])
