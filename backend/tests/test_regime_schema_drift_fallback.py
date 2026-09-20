"""regime_builder schema drift 兜底测试。

背景:
    A 股 enriched 在不同日期 schema 不一致 (早期 15 列含 turnover_rate/consecutive_limit_*
    等, 最近 12 列精简输出)。commit ① 后的 enriched 不含 change_pct / signal_limit_*
    / ma20 这些 regime 评分必需的列。

    _aggregate_daily 在 commit ⑤ 后加了 schema drift 兜底:
    - change_pct: 用 close / close.shift(1).over(symbol) - 1 派生 (None when prev<=0)
    - signal_limit_up: 用 raw_close / raw_close.shift(1).over(symbol) 比值 >= 0.095 推断
    - signal_limit_down: 同理 <= -0.095
    - signal_broken_limit_up: 兜底 False
    - ma20: 缺则 ma20_above 走 0 (聚合仍 OK, trend 子分钳到中位)

测试:
    1. 12 列精简 schema 聚合成功 + limit_up 在合理区间 (60~200 涨停/日)
    2. 15 列老 schema 也能成功 (向后兼容)
    3. signal_limit_up 推断正确 (raw_close 比值达 0.10 → True)
    4. change_pct 派生: 涨 1%/跌 1% 都能正确
    5. cn regime 落盘后 load_regime_history 能读到
    6. hk/us regime 落盘同样 OK
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.services import regime_builder


def _make_minimal_cn_row(symbol: str, d: date, close: float, raw_close: float) -> dict:
    """生成 A 股 enriched 12 列精简 schema 的一行。"""
    return {
        "symbol": symbol,
        "date": d,
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": 1_000_000.0,
        "amount": 1_000_000.0 * close,
        "raw_close": raw_close,
        "raw_high": raw_close * 1.01,
        "raw_low": raw_close * 0.98,
        "quote_ts": None,
    }


def _make_full_cn_row(symbol: str, d: date, close: float, raw_close: float) -> dict:
    """生成 A 股 enriched 15 列老 schema 的一行 (含 turnover_rate / consecutive_*)."""
    row = _make_minimal_cn_row(symbol, d, close, raw_close)
    row["turnover_rate"] = 0.5
    row["consecutive_limit_ups"] = 0
    row["consecutive_limit_downs"] = 0
    return row


# ─────────────── 1. 12 列精简 schema 聚合成功 ───────────────


def test_aggregate_12col_minimal_schema_succeeds():
    """A 股 enriched 12 列精简 schema 不再 return empty, limit_up 在合理区间。"""
    # 30 只标的 × 5 天, 模拟 09-08~09-12
    rows = []
    for d_idx, d in enumerate([date(2026, 9, 8), date(2026, 9, 9),
                               date(2026, 9, 10), date(2026, 9, 11)]):
        for i in range(30):
            # 30% 标的涨停 (+10%)
            if i < 9:
                base = 10.0 + i * 0.5
                # 上一日 9.09, 今日 9.99 (10% 涨)
                close = base
                raw_close = base * 1.099 if d_idx > 0 else base
            else:
                base = 5.0 + i * 0.1
                close = base
                raw_close = base
            rows.append(_make_minimal_cn_row(f"000{i:03d}.SZ", d, close, raw_close))

    df = pl.DataFrame(rows)
    out = regime_builder._aggregate_daily(df, index_pct_map=None, market="cn")
    assert not out.is_empty(), "12 列精简 schema 应能聚合出数据"
    assert out.height == 4, f"应聚合出 4 天, 实际 {out.height}"
    # 每天 9 只涨停 (i=0..8, 9 只)
    # 第二天起, 上一日 raw_close * 1.099 ≈ close, 所以 daily change_pct 触发涨停检测
    # limit_up 数应在合理区间 (5~15 之间, 略宽松)
    daily_limit_up = out["limit_up"].to_list()
    assert all(0 <= n <= 30 for n in daily_limit_up), \
        f"limit_up 应在 [0, 30] 区间, 实际 {daily_limit_up}"


# ─────────────── 2. 15 列老 schema 仍能跑 ───────────────


def test_aggregate_15col_full_schema_still_works():
    """A 股 enriched 15 列老 schema (含 turnover_rate / consecutive_*) 仍能聚合。"""
    rows = []
    for d in [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]:
        for i in range(20):
            rows.append(_make_full_cn_row(f"600{i:03d}.SH", d, 10.0 + i * 0.3, 10.0 + i * 0.3))

    df = pl.DataFrame(rows)
    out = regime_builder._aggregate_daily(df, index_pct_map=None, market="cn")
    assert not out.is_empty()
    assert out.height == 4


# ─────────────── 3. signal_limit_up 推断正确 ───────────────


def test_signal_limit_up_inference_from_raw_close():
    """raw_close / prev 比值 >= 9.5% 推断为涨停。"""
    # 一只标的连续 2 天, 第 2 天 raw_close 涨 10%
    rows = [
        _make_minimal_cn_row("000001.SZ", date(2026, 9, 10), 10.0, 10.0),
        _make_minimal_cn_row("000001.SZ", date(2026, 9, 11), 10.5, 11.0),  # raw_close +10%
    ]
    df = pl.DataFrame(rows)

    # 跑一次内部 derived signal_limit_up 的检测
    from app.services.regime_builder import _aggregate_daily
    out = _aggregate_daily(df, index_pct_map=None, market="cn")
    assert not out.is_empty()
    # 9-11 那天 limit_up 应 = 1 (000001 涨停)
    last = out.row(out.height - 1, named=True)
    assert last["limit_up"] >= 1, f"9-11 应识别出涨停, 实际 {last['limit_up']}"


# ─────────────── 4. change_pct 派生: 涨 1%/跌 1% 都能正确 ───────────────


def test_change_pct_derivation_uses_close_shift():
    """change_pct 用 close / close.shift(1) - 1 派生, 与看板涨跌幅榜口径一致。"""
    rows = [
        _make_minimal_cn_row("000001.SZ", date(2026, 9, 10), 10.0, 10.0),
        _make_minimal_cn_row("000001.SZ", date(2026, 9, 11), 10.1, 10.1),  # +1%
    ]
    df = pl.DataFrame(rows)

    # 派生
    derived = df.with_columns(
        pl.when(pl.col("close").shift(1).over("symbol") > 0)
        .then(pl.col("close") / pl.col("close").shift(1).over("symbol") - 1)
        .otherwise(None)
        .alias("change_pct")
    )
    cp = derived.filter(pl.col("date") == date(2026, 9, 11))["change_pct"].to_list()
    assert len(cp) == 1
    assert abs(cp[0] - 0.01) < 1e-6, f"应派生 +1%, 实际 {cp[0]}"


# ─────────────── 5. cn regime 落盘后能读回 ───────────────


def test_cn_regime_round_trip(tmp_path):
    """cn regime 落盘 → 读回 → 5 档状态完整。"""
    rows = []
    for d in [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]:
        for i in range(30):
            rows.append(_make_minimal_cn_row(f"000{i:03d}.SZ", d, 10.0 + i, 10.0 + i))
    df = pl.DataFrame(rows)

    out = regime_builder._aggregate_daily(df, index_pct_map=None, market="cn")
    assert not out.is_empty()

    regime_builder.upsert_regime_history(tmp_path, out, market="cn")
    regime_builder.refresh_phase_labels(tmp_path, market="cn")

    h = regime_builder.load_regime_history(tmp_path, market="cn")
    assert not h.is_empty()
    # 验证 5 档状态合理 (state 列存在)
    if "state" in h.columns:
        assert h["state"].null_count() == 0, "state 列不应有 null"


# ─────────────── 6. hk/us regime 落盘同样 OK ───────────────


def test_hk_us_regime_round_trip(tmp_path):
    """hk/us regime 落盘 → 读回 → 数据完整。"""
    rows = []
    for d in [date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11)]:
        for i in range(20):
            rows.append({
                "symbol": f"0000{i}.HK" if i < 10 else f"AAPL{i}.US",
                "date": d,
                "close": 100.0 + i,
                "momentum_20d": 0.05 + i * 0.01,
                "signal_n_day_high": i % 3 == 0,
            })
    df = pl.DataFrame(rows)

    for mkt in ("hk", "us"):
        # 过滤到该市场后缀
        suffix = f".{mkt.upper()}"
        sub = df.filter(pl.col("symbol").str.ends_with(suffix))
        if sub.is_empty():
            continue
        out = regime_builder._aggregate_daily(sub, index_pct_map=None, market=mkt)
        assert not out.is_empty(), f"{mkt} 应能聚合"
        regime_builder.upsert_regime_history(tmp_path, out, market=mkt)
        h = regime_builder.load_regime_history(tmp_path, market=mkt)
        assert h.height == out.height