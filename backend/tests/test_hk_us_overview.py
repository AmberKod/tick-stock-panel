"""港美 overview builder (阶段 B-2) 单测。

覆盖: 结构完整性 (对齐 A股 overview schema)、空数据分支、聚合正确性、
以及最新交易日样本的**覆盖率**(幸存者偏差显式化)。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.services.hk_us_overview_builder import (
    LatestRows,
    _load_latest_rows,
    build_hk_us_overview,
)

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


# ---------------------------------------------------------------
# 样本覆盖率 (幸存者偏差显式化)
# ---------------------------------------------------------------

def _write_enriched_rows(tmp_path, symbol: str, rows: list[tuple[str, float]]) -> None:
    """给单个 symbol 写 enriched 分区;rows = [(YYYY-MM-DD, change_pct), ...] 按日期递增。"""
    df = pl.DataFrame({
        "symbol": [symbol] * len(rows),
        "date": [datetime.fromisoformat(d) for d, _ in rows],
        "open": [10.0] * len(rows), "high": [12.0] * len(rows), "low": [9.0] * len(rows),
        "close": [10.0] * len(rows), "volume": [1e6] * len(rows),
        "amount": [1e7] * len(rows), "prev_close": [10.0] * len(rows),
        "ma5": [9.9] * len(rows), "ma20": [9.5] * len(rows), "ma60": [9.0] * len(rows),
        "high_60d": [15.0] * len(rows), "low_60d": [5.0] * len(rows),
        "vol_ratio_5d": [1.2] * len(rows), "annual_vol_20d": [0.25] * len(rows),
        "change_pct": [pct for _, pct in rows],
        "change_amount": [0.1] * len(rows), "amplitude": [0.03] * len(rows),
        "signal_n_day_high": [False] * len(rows),
        "signal_n_day_low": [False] * len(rows),
        "signal_volume_surge": [False] * len(rows),
    })
    out = tmp_path / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(out)


def test_coverage_exposes_partial_sample(tmp_path):
    """只有部分标的更新到最新交易日时, 覆盖率与 stale 数必须显式算出。

    布局: A/B/C 三只更新到 09-18 (as_of), D/E 停在 09-03, universe 6 只
    (含没有 enriched 行的 F)。期望 covered=3 / universe=6 → ratio=0.5,
    stale_symbols=2 停在 2026-09-03 —— 而不是把这 2 只静默丢掉。
    """
    for sym in ("00700", "09988", "00001"):
        _write_enriched_rows(tmp_path, f"{sym}.HK", [("2026-09-03", 0.01), ("2026-09-18", 0.02)])
    for sym in ("00002", "00003"):
        _write_enriched_rows(tmp_path, f"{sym}.HK", [("2026-09-03", -0.01)])
    _write_fake_instruments(tmp_path, "HK", ["00700", "09988", "00001", "00002", "00003", "00004"])

    snapshot = _load_latest_rows(tmp_path, "HK")
    assert isinstance(snapshot, LatestRows)
    assert snapshot.as_of == date(2026, 9, 18)
    assert snapshot.rows.height == 3  # 只有更新到 09-18 的三只进入本次排名

    coverage = snapshot.coverage
    assert coverage is not None and coverage["covered"] == 3
    assert coverage["universe"] == 6
    assert coverage["ratio"] == 0.5
    assert coverage["as_of"] == "2026-09-18"
    assert coverage["stale_symbols"] == 2
    assert coverage["stale_as_of"] == "2026-09-03"
    # 分布也是计数值: 停在哪天各有多少只, 便于文案说清"最多一档停在 xx"
    assert coverage["stale_buckets"] == [{"date": "2026-09-03", "count": 2}]


def test_coverage_denominator_comes_from_instruments_parquet(tmp_path):
    """分母必须来自真实读盘的 ``instruments/hk_instruments.parquet``,不是命中数。

    历史问题: 热点测试几乎全是离线注入 loader、零端到端真实 parquet。这里刻意
    走"写 parquet → 读 parquet"的完整路径,并断言三件事:
      1) 在 universe 里但**完全没有 enriched 行**的标的也进分母(它是 universe
         的一部分,不算"停在旧日期",所以不进 stale_symbols);
      2) 改写 parquet 后分母跟着变 —— 证明分母读的就是这个文件;
      3) 别市场的标的(非 .HK 后缀)不混进分母。
    """
    _write_enriched_rows(tmp_path, "00700.HK", [("2026-09-18", 0.02)])
    _write_fake_instruments(tmp_path, "HK", ["00700", "00004"])  # 00004 无行情

    first = _load_latest_rows(tmp_path, "HK").coverage
    assert first["covered"] == 1
    assert first["universe"] == 2
    assert first["ratio"] == 0.5
    assert first["stale_symbols"] == 0  # 从没有数据 ≠ 停在旧日期

    # 扩 universe → 分母跟着变
    _write_fake_instruments(tmp_path, "HK", ["00700", "00004", "00005", "00006"])
    second = _load_latest_rows(tmp_path, "HK").coverage
    assert second["covered"] == 1
    assert second["universe"] == 4
    assert second["ratio"] == 0.25

    # 别市场的标的不能混进分母(按后缀过滤)
    mixed = pl.DataFrame({"symbol": ["00700.HK", "AAPL.US"], "name": ["港", "美"]})
    mixed.write_parquet(tmp_path / "instruments" / "hk_instruments.parquet")
    third = _load_latest_rows(tmp_path, "HK").coverage
    assert third["covered"] == 1
    assert third["universe"] == 1
    assert third["ratio"] == 1.0


def test_coverage_is_none_when_universe_unreadable(tmp_path):
    """分母不可得 → 覆盖率显式 None, 不拿命中数冒充 universe (不可用不计入分母)。"""
    _write_enriched_rows(tmp_path, "00700.HK", [("2026-09-18", 0.02)])
    snapshot = _load_latest_rows(tmp_path, "HK")
    assert snapshot.as_of == date(2026, 9, 18)
    assert snapshot.coverage is None


def test_coverage_none_when_no_enriched_data(tmp_path):
    snapshot = _load_latest_rows(tmp_path, "HK")
    assert snapshot.as_of is None
    assert snapshot.coverage is None
    assert snapshot.rows.height == 0


def test_build_overview_ignores_coverage_without_changing_schema(tmp_path):
    """overview 的对外 schema 不受覆盖率计算影响 (结构回归)。"""
    _write_enriched_rows(tmp_path, "00700.HK", [("2026-09-18", 0.06)])
    _write_fake_instruments(tmp_path, "HK", ["00700"])
    ov = build_hk_us_overview("HK", tmp_path)
    for key in _OVERVIEW_KEYS:
        assert key in ov
    assert ov["breadth"]["total"] == 1
    assert ov["as_of"] == "2026-09-18"
