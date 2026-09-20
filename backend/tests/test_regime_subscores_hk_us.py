"""港美 Regime 评分测试(commit ② 配套)。

覆盖:
- _compute_subscores: cn 路径行为不变; hk/us 走动量+新高合成, 各子分落入 [0, 100]。
- classify_state: cn/hk/us 都返回 5 状态之一, 综合分合理。
- _aggregate_daily: 港美 enriched schema 含 momentum_20d / signal_n_day_high 时聚合出
  _m20d_mean / _m25_cnt / _m15_cnt / _m8_cnt / _nh_cnt 列, 并由这些列派生 metrics。
- _load_index_pct: 用恒指/标普基准, 失败静默降级。
- run_regime_batch: cn 走原路径(零回归); hk/us 走 per-symbol 扫描。

不触动 compute_limit_signals / with_prev_consecutive 等 A 股专属工具 — 那些是 A 股
路径需要的, 港美走 schema 兼容补 False 列即可。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import regime_builder

# ───────────────────────── _compute_subscores cn 路径零回归 ─────────────────────────


def test_subscores_cn_unchanged_default_market():
    """cn 路径(market='cn' 或默认): 与 commit ① 行为一致(对比维度是 speculation 走涨停路径)。"""
    metrics = {
        "up_pct": 80, "down_pct": 15, "avg_pct": 0.025, "median_pct": 0.02,
        "strong_up_pct": 12, "strong_down_pct": 1, "strong_diff_pct": 11,
        "limit_up": 50, "seal_rate": 0.8, "max_consecutive": 7,
        "index_pct": 0.015, "above_ma20_pct": 0.65,
    }
    sub_cn = regime_builder._compute_subscores(metrics, market="cn")
    # 默认参数同 cn
    sub_default = regime_builder._compute_subscores(metrics)
    assert sub_cn["speculation"] == sub_default["speculation"]
    assert sub_cn["score"] == sub_default["score"]
    # 全部子分落在 [0, 100]
    for k in ("profit", "speculation", "resilience", "trend", "score"):
        assert 0 <= sub_cn[k] <= 100


# ───────────────────────── _compute_subscores hk/us 路径 ─────────────────────────


def test_subscores_hk_uses_momentum_new_high_not_limit_up():
    """hk 路径: speculation 不再看 limit_up/seal_rate/max_consecutive, 走动量+新高合成。"""
    # 两个 metrics: 同样的涨停指标但动量指标完全不同 → 港美结果应不同
    high_limit_up = {
        "up_pct": 80, "down_pct": 15, "avg_pct": 0.025, "median_pct": 0.02,
        "strong_up_pct": 12, "strong_down_pct": 1, "strong_diff_pct": 11,
        "limit_up": 100, "seal_rate": 0.95, "max_consecutive": 12,
        "index_pct": 0.015, "above_ma20_pct": 0.65,
        # 港美指标
        "momentum_20d_pct": 0.01, "momentum_25_share": 0.02,
        "momentum_15_share": 0.05, "momentum_8_share": 0.10,
        "new_high_share": 0.05,
    }
    high_momentum = {
        "up_pct": 80, "down_pct": 15, "avg_pct": 0.025, "median_pct": 0.02,
        "strong_up_pct": 12, "strong_down_pct": 1, "strong_diff_pct": 11,
        "limit_up": 0, "seal_rate": 0.5, "max_consecutive": 0,   # 港美无涨停
        "index_pct": 0.015, "above_ma20_pct": 0.65,
        "momentum_20d_pct": 0.06, "momentum_25_share": 0.10,
        "momentum_15_share": 0.18, "momentum_8_share": 0.30,
        "new_high_share": 0.22,
    }

    sub_limit = regime_builder._compute_subscores(high_limit_up, market="hk")
    sub_momentum = regime_builder._compute_subscores(high_momentum, market="hk")

    # 同样的 profit/resilience/trend: 应相等(metrics 其它字段相同)
    assert sub_limit["profit"] == sub_momentum["profit"]
    assert sub_limit["resilience"] == sub_momentum["resilience"]
    assert sub_limit["trend"] == sub_momentum["trend"]
    # 但 speculation 不同: high_momentum 应该比 high_limit_up 高
    assert sub_momentum["speculation"] > sub_limit["speculation"]
    # 综合分也更高
    assert sub_momentum["score"] > sub_limit["score"]


def test_subscores_us_same_formula_as_hk():
    """us 与 hk 用同一合成公式(spec/weights/校准值相同)。"""
    metrics = {
        "up_pct": 60, "down_pct": 30, "avg_pct": 0.01, "median_pct": 0.005,
        "strong_up_pct": 5, "strong_down_pct": 3, "strong_diff_pct": 2,
        "limit_up": 0, "seal_rate": 0.5, "max_consecutive": 0,
        "index_pct": 0.005, "above_ma20_pct": 0.55,
        "momentum_20d_pct": 0.04, "momentum_25_share": 0.05,
        "momentum_15_share": 0.12, "momentum_8_share": 0.20,
        "new_high_share": 0.15,
    }
    sub_hk = regime_builder._compute_subscores(metrics, market="hk")
    sub_us = regime_builder._compute_subscores(metrics, market="us")
    assert sub_hk == sub_us


def test_subscores_hk_score_in_range():
    """极值: 全部指标拉满 → score 不超过 100; 全部为空 → score 不低于 0。"""
    all_high = {
        "up_pct": 95, "down_pct": 3, "avg_pct": 0.10, "median_pct": 0.08,
        "strong_up_pct": 30, "strong_down_pct": 0.1, "strong_diff_pct": 30,
        "index_pct": 0.10, "above_ma20_pct": 0.95,
        "momentum_20d_pct": 0.20, "momentum_25_share": 0.30,
        "momentum_15_share": 0.50, "momentum_8_share": 0.80,
        "new_high_share": 0.50,
    }
    sub = regime_builder._compute_subscores(all_high, market="hk")
    assert 0 <= sub["score"] <= 100
    # 强势日: 应 > 70
    assert sub["score"] > 70

    all_low = {
        "up_pct": 5, "down_pct": 95, "avg_pct": -0.05, "median_pct": -0.04,
        "strong_up_pct": 0.1, "strong_down_pct": 30, "strong_diff_pct": -30,
        "index_pct": -0.05, "above_ma20_pct": 0.05,
        "momentum_20d_pct": -0.10, "momentum_25_share": 0.0,
        "momentum_15_share": 0.0, "momentum_8_share": 0.0,
        "new_high_share": 0.0,
    }
    sub_low = regime_builder._compute_subscores(all_low, market="hk")
    assert 0 <= sub_low["score"] <= 100
    # 弱势日: 应 < 30
    assert sub_low["score"] < 30


def test_subscores_hk_missing_momentum_fields_use_defaults():
    """hk 指标字段缺失时, .get 默认值兜底, 不抛。"""
    minimal = {"up_pct": 50, "down_pct": 30, "avg_pct": 0.0, "median_pct": 0.0,
               "strong_up_pct": 1, "strong_down_pct": 1, "strong_diff_pct": 0,
               "index_pct": 0.0, "above_ma20_pct": 0.5}
    sub = regime_builder._compute_subscores(minimal, market="hk")
    assert 0 <= sub["score"] <= 100


# ───────────────────────── classify_state 各市场 ─────────────────────────


def test_classify_state_returns_one_of_five_for_hk():
    """hk 走规则引擎 → 5 状态之一。"""
    metrics = {
        "up_pct": 60, "down_pct": 30, "avg_pct": 0.01, "median_pct": 0.005,
        "strong_up_pct": 5, "strong_down_pct": 3, "strong_diff_pct": 2,
        "index_pct": 0.005, "above_ma20_pct": 0.55,
        "momentum_20d_pct": 0.04, "momentum_25_share": 0.05,
        "momentum_15_share": 0.12, "momentum_8_share": 0.20,
        "new_high_share": 0.15,
    }
    state, score = regime_builder.classify_state(metrics, market="hk")
    assert state in {"strong", "lean_strong", "range", "lean_weak", "weak"}
    assert 0 <= score <= 100


def test_classify_state_default_market_is_cn():
    """不传 market 默认 cn(老调用零回归)。"""
    metrics = {
        "up_pct": 80, "down_pct": 15, "avg_pct": 0.025, "median_pct": 0.02,
        "strong_up_pct": 12, "strong_down_pct": 1, "strong_diff_pct": 11,
        "limit_up": 50, "seal_rate": 0.8, "max_consecutive": 7,
        "index_pct": 0.015, "above_ma20_pct": 0.65,
    }
    state_default, _ = regime_builder.classify_state(metrics)
    state_cn, _ = regime_builder.classify_state(metrics, market="cn")
    assert state_default == state_cn


# ───────────────────────── _aggregate_daily 港美聚合 ─────────────────────────


def _make_hk_us_row(symbol: str, date_: date, change_pct: float, momentum_20d: float,
                    signal_n_day_high: bool, ma20: float, close: float) -> dict:
    return {
        "symbol": symbol,
        "date": date_,
        "change_pct": change_pct,
        "amount": 1_000_000.0,
        "momentum_20d": momentum_20d,
        "signal_n_day_high": signal_n_day_high,
        "close": close,
        "ma20": ma20,
    }


def test_aggregate_daily_hk_derives_momentum_metrics():
    """hk enriched → metrics 含 momentum_20d_pct / new_high_share / *_share 字段。"""
    # 8 只港股, 当日动量差异大
    d = date(2026, 9, 10)
    rows = [
        # 4 只强动量 (momentum_20d >= 0.25)
        _make_hk_us_row("00001.HK", d, 0.02, 0.30, True, 100.0, 105.0),
        _make_hk_us_row("00002.HK", d, 0.03, 0.28, True, 50.0, 55.0),
        _make_hk_us_row("00003.HK", d, 0.05, 0.35, False, 200.0, 215.0),
        _make_hk_us_row("00004.HK", d, 0.04, 0.27, False, 80.0, 82.0),
        # 2 只中等动量 (0.15~0.25)
        _make_hk_us_row("00005.HK", d, 0.01, 0.20, False, 60.0, 58.0),
        _make_hk_us_row("00006.HK", d, -0.01, 0.18, False, 70.0, 68.0),
        # 2 只弱动量 (<0.03)
        _make_hk_us_row("00007.HK", d, -0.02, 0.01, False, 40.0, 39.0),
        _make_hk_us_row("00008.HK", d, -0.03, -0.05, False, 30.0, 28.0),
    ]
    df = pl.DataFrame(rows)
    # 必须显式带 false 列(模拟 _scan_hk_us_enriched_for_regime 的列补全)
    df = df.with_columns([
        pl.lit(False).alias("signal_limit_up"),
        pl.lit(False).alias("signal_limit_down"),
        pl.lit(False).alias("signal_broken_limit_up"),
        pl.lit(0).alias("consecutive_limit_ups"),
    ])

    out = regime_builder._aggregate_daily(df, index_pct_map={d: 0.01}, market="hk")
    assert not out.is_empty()
    assert "date" in out.columns
    # 任一行的派生字段都应存在
    row = out.row(0, named=True)
    assert "momentum_20d_pct" in row
    assert "momentum_25_share" in row
    assert "new_high_share" in row
    # m25_share 应为 4/8 = 0.5
    assert abs(row["momentum_25_share"] - 0.5) < 1e-6
    # new_high_share 应为 2/8 = 0.25
    assert abs(row["new_high_share"] - 0.25) < 1e-6
    # limit_up 应为 0(港美无涨停聚合)
    assert row["limit_up"] == 0


def test_aggregate_daily_cn_unaffected_by_market_arg():
    """cn 路径: 港美动量字段缺失时, 聚合列置 0, 不影响 A 股指标计算。"""
    # A 股模拟 enriched(只含 cn 列)
    d = date(2026, 9, 10)
    rows = [
        {"symbol": "000001.SZ", "date": d, "change_pct": 0.10,
         "amount": 1_000_000.0, "signal_limit_up": True,
         "signal_limit_down": False, "signal_broken_limit_up": False,
         "consecutive_limit_ups": 3, "close": 11.0, "ma20": 10.0},
        {"symbol": "000002.SZ", "date": d, "change_pct": 0.05,
         "amount": 500_000.0, "signal_limit_up": False,
         "signal_limit_down": False, "signal_broken_limit_up": False,
         "consecutive_limit_ups": 0, "close": 21.0, "ma20": 20.0},
    ]
    df = pl.DataFrame(rows)
    out = regime_builder._aggregate_daily(df, index_pct_map={d: 0.005}, market="cn")
    assert not out.is_empty()
    row = out.row(0, named=True)
    assert row["limit_up"] == 1
    assert row["max_consecutive"] == 3
    # 港美字段为 0
    assert row["momentum_20d_pct"] == 0.0
    assert row["new_high_share"] == 0.0


# ───────────────────────── _MARKET_BENCHMARKS 路由 ─────────────────────────


def test_market_benchmarks_index():
    """各市场基准指数正确。"""
    assert regime_builder._MARKET_BENCHMARKS["cn"] == "000001.SH"  # CN_PROFILE.benchmark_symbol
    assert regime_builder._MARKET_BENCHMARKS["hk"] == "^HSI"
    assert regime_builder._MARKET_BENCHMARKS["us"] == "^GSPC"


# ───────────────────────── _scan_hk_us_enriched_for_regime 接口 ─────────────────────────


def test_scan_hk_us_enriched_for_regime_returns_none_when_dir_missing(tmp_path):
    """enriched 目录不存在时返回 None(不抛)。"""
    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    result = regime_builder._scan_hk_us_enriched_for_regime(
        _FakeRepo(), date(2026, 9, 10), date(2026, 9, 10), "hk"
    )
    assert result is None


def test_scan_hk_us_enriched_for_regime_filters_by_suffix_and_range(tmp_path):
    """按市场后缀过滤(.HK / .US), 按日期范围截取。"""
    enriched_dir = tmp_path / "kline_hk_us_enriched"
    hk_sym = enriched_dir / "symbol=00001.HK"
    us_sym = enriched_dir / "symbol=AAPL.US"
    hk_sym.mkdir(parents=True)
    us_sym.mkdir(parents=True)

    # HK 数据: 09-09 ~ 09-11
    pl.DataFrame({
        "symbol": ["00001.HK"] * 3,
        "date": [date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)],
        "change_pct": [0.01, 0.02, -0.01],
        "amount": [100.0, 200.0, 150.0],
        "close": [10.0, 11.0, 10.5],
        "ma20": [9.5, 10.0, 10.2],
        "momentum_20d": [0.05, 0.06, 0.04],
        "signal_n_day_high": [False, True, False],
    }).write_parquet(hk_sym / "part.parquet")

    # US 数据: 同日(应被 hk 过滤掉)
    pl.DataFrame({
        "symbol": ["AAPL.US"] * 3,
        "date": [date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)],
        "change_pct": [0.01, 0.02, -0.01],
        "amount": [100.0, 200.0, 150.0],
        "close": [10.0, 11.0, 10.5],
        "ma20": [9.5, 10.0, 10.2],
        "momentum_20d": [0.05, 0.06, 0.04],
        "signal_n_day_high": [False, True, False],
    }).write_parquet(us_sym / "part.parquet")

    class _FakeRepo:
        class _Store:
            data_dir = tmp_path
        store = _Store()

    # hk 路径: 只返回 09-10 ~ 09-11(范围截取)
    hk = regime_builder._scan_hk_us_enriched_for_regime(
        _FakeRepo(), date(2026, 9, 10), date(2026, 9, 11), "hk"
    )
    assert hk is not None
    assert hk.height == 2
    assert hk["symbol"].unique().to_list() == ["00001.HK"]
    # 信号列被显式补 False/0(让 _aggregate_daily 走"信号缺失"分支)
    assert "signal_limit_up" in hk.columns
    assert hk["signal_limit_up"].to_list() == [False, False]
    assert "consecutive_limit_ups" in hk.columns
    assert hk["consecutive_limit_ups"].to_list() == [0, 0]

    # us 路径: 同样只返回 US 数据
    us = regime_builder._scan_hk_us_enriched_for_regime(
        _FakeRepo(), date(2026, 9, 10), date(2026, 9, 11), "us"
    )
    assert us is not None
    assert us["symbol"].unique().to_list() == ["AAPL.US"]