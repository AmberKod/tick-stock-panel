"""HK provider 09-15 修复回归: 坏行剔除 + 腾讯补位窗口收缩。

背景: 全量补跑 2811 只仅 31 只落盘。抽样实证 13/20 只 sina 全历史含少量坏行
(汇丰 8 行/港交所 22 行级别), `_validated_rows` 单行坏即整只抛错 → primary 全废;
腾讯补位又被深历史 (1998 起) WAF 501 拒 → 两源皆空 → empty_result (2533 只)。

修复: ①坏行剔除+计数(不再整只抛错), fallback 补位或 coverage 报 missing;
②primary 非空时腾讯只拉近 120 天(币种+交叉核验+近段), primary 整体失败才全窗。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.data_providers import hk_daily_provider as mod
from app.data_providers.hk_daily_provider import _validated_rows


def _row(day: str, o=10.0, h=11.0, low=9.0, c=10.5, v=1000.0):
    return {"date": day, "open": o, "high": h, "low": low, "close": c, "volume": v}


GOOD = [
    _row("2026-09-10"),
    _row("2026-09-11"),
    _row("2026-09-14"),
    _row("2026-09-15"),
]


def test_bad_ohlc_row_dropped_not_whole_symbol(caplog):
    """低点高于收盘 (OHLC 关系无效) 的单行被剔除, 其余行保留。"""
    rows = [ *_row_old_wrap(), _row("2010-06-01", o=10.0, h=11.0, low=12.0, c=10.5), *GOOD ]
    with caplog.at_level("WARNING"):
        frame = _validated_rows(rows, "00005.HK", "sina_hk_daily", "2026-09-15T00:00:00Z")
    assert frame.height == len(rows) - 1
    assert "2010-06-01" not in frame["date"].dt.strftime("%Y-%m-%d").to_list()
    assert any("剔除" in r.message for r in caplog.records)


def _row_old_wrap():
    # 深历史好行 (2010 年代), 模拟 28 年历史主体
    return [
        _row("2008-01-02"),
        _row("2008-01-03"),
        _row("2009-06-01"),
    ]


def test_non_finite_and_negative_volume_rows_dropped():
    rows = [
        _row("2026-09-10", o=float("nan")),
        _row("2026-09-11", v=-5.0),
        _row("2026-09-14"),
    ]
    frame = _validated_rows(rows, "00005.HK", "sina_hk_daily", "2026-09-15T00:00:00Z")
    assert frame.height == 1
    assert frame["date"][0] == date(2026, 9, 14)


def test_identity_and_future_date_still_raise():
    """身份错混与未来日期仍是硬错误 (不可剔除)。"""
    with pytest.raises(ValueError, match="其他证券代码"):
        _validated_rows(
            [{**_row("2026-09-10"), "symbol": "00388.HK"}],
            "00005.HK", "sina_hk_daily", "2026-09-15T00:00:00Z",
        )
    tomorrow = (datetime.now(mod.HK_TZ).date() + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="未来"):
        _validated_rows([_row(tomorrow)], "00005.HK", "sina_hk_daily", "2026-09-15T00:00:00Z")


def test_all_rows_bad_returns_empty_frame():
    """全部行坏 → 空 frame (走 fallback), 不抛错。"""
    rows = [
        _row("2026-09-10", low=99.0),
        _row("2026-09-11", low=99.0),
    ]
    frame = _validated_rows(rows, "00005.HK", "sina_hk_daily", "2026-09-15T00:00:00Z")
    assert frame.is_empty()


def test_tencent_fallback_window_shrinks_when_primary_ok(monkeypatch):
    """primary 非空时腾讯窗口 = 近 120 天 (不再从 1998 拉全历史)。"""
    provider = mod.HKDailyProvider()

    def fake_sina(client, symbol, observed_at, start=None, end=None):
        return pl.DataFrame({
            "symbol": ["00005.HK"], "date": [date(2026, 9, 15)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0],
            "amount": [None], "source": ["sina_hk_daily"],
            "price_adjustment": ["unadjusted"], "amount_source": [None],
            "volume_unit": ["share"], "currency": [None],
            "price_schema_version": [1], "observed_at": [observed_at], "raw_price_verified": [True],
        }).with_columns(pl.lit(date(1998, 6, 1), dtype=pl.Date).alias("source_first_date"))

    tencent_calls: list[tuple[date, date]] = []

    def fake_tencent(client, symbol, start, end, observed_at):
        tencent_calls.append((start, end))
        frame = pl.DataFrame({
            "symbol": ["00005.HK"], "date": [date(2026, 9, 15)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0],
            "amount": [None], "source": ["tencent_hk_daily"],
            "price_adjustment": ["unadjusted"], "amount_source": [None],
            "volume_unit": ["share"], "currency": [None],
            "price_schema_version": [1], "observed_at": [observed_at], "raw_price_verified": [True],
        })
        return frame, "HKD"

    def fake_calendar(client, start, end):
        return {date(2026, 9, 15)}

    monkeypatch.setattr(provider, "_sina", fake_sina)
    monkeypatch.setattr(provider, "_tencent", fake_tencent)
    monkeypatch.setattr(provider, "_calendar", fake_calendar)
    monkeypatch.setattr(mod, "_get_text", lambda client, url: "")

    result = provider.get_daily_with_report(
        ["00005.HK"], datetime(1998, 6, 1), datetime(2026, 9, 16), "stock",
    )
    assert tencent_calls, "腾讯应被调用"
    fb_start, _unused_end = tencent_calls[0]
    assert fb_start >= date(2026, 5, 15), f"腾讯窗口应收缩到近120天, 实际从 {fb_start}"
    assert result.frame.height >= 1


def test_tencent_full_window_when_primary_empty(monkeypatch):
    """primary 整体失败时腾讯保持完整窗口 (仍是唯一来源)。"""
    provider = mod.HKDailyProvider()

    def failing_sina(client, symbol, observed_at, start=None, end=None):
        raise ValueError("sina down")

    tencent_calls: list[tuple[date, date]] = []

    def fake_tencent(client, symbol, start, end, observed_at):
        tencent_calls.append((start, end))
        frame = pl.DataFrame({
            "symbol": ["00005.HK"], "date": [date(2026, 9, 15)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5], "volume": [100.0],
            "amount": [None], "source": ["tencent_hk_daily"],
            "price_adjustment": ["unadjusted"], "amount_source": [None],
            "volume_unit": ["share"], "currency": [None],
            "price_schema_version": [1], "observed_at": [observed_at], "raw_price_verified": [True],
        })
        return frame, "HKD"

    def fake_calendar(client, start, end):
        return {date(2026, 9, 15)}

    monkeypatch.setattr(provider, "_sina", failing_sina)
    monkeypatch.setattr(provider, "_tencent", fake_tencent)
    monkeypatch.setattr(provider, "_calendar", fake_calendar)
    monkeypatch.setattr(mod, "_get_text", lambda client, url: "")

    provider.get_daily_with_report(
        ["00005.HK"], datetime(2020, 1, 1), datetime(2026, 9, 16), "stock",
    )
    fb_start, _ = tencent_calls[0]
    assert fb_start == date(2020, 1, 1), "primary 失败时腾讯应保持原完整窗口"


def _raw_frame(days: list[date], open_: float = 10.0, source: str = "sina_hk_daily") -> pl.DataFrame:
    n = len(days)
    return pl.DataFrame({
        "symbol": ["00005.HK"] * n, "date": days,
        "open": [open_] * n, "high": [11.0] * n, "low": [9.0] * n, "close": [10.5] * n,
        "volume": [100.0] * n, "amount": [None] * n, "source": [source] * n,
        "price_adjustment": ["unadjusted"] * n, "amount_source": [None] * n,
        "volume_unit": ["share"] * n, "currency": [None] * n,
        "price_schema_version": [1] * n,
        "observed_at": ["t"] * n, "raw_price_verified": [True] * n,
    })


def test_merge_raw_minor_conflict_keeps_primary(caplog):
    """重叠区仅 1 行冲突 (≤3 且 ≤5%) → 取主源, 不废整只也不留 coverage 缺口。

    09-15 实测 00005.HK/00700.HK: 84 行重叠仅 2026-09-04 一日 open 不同
    (166.9 vs 166.0), high/low/close/volume 完全一致 —— 同一场交易的报价精度
    差异。整日剔除会让 coverage 报 missing, `publish_hk_daily_snapshot` 因此
    跳过 enriched 重算, 代价远大于保留主源开盘价。
    """
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(84)]
    primary = _raw_frame(days)
    # 构造 fallback 与 primary 基本一致, 仅单行开盘差 0.9
    fb_same = _raw_frame(days, source="tencent_hk_daily")
    bad_day = days[40]
    fb_single = fb_same.with_columns(
        pl.when(pl.col("date") == bad_day).then(pl.lit(11.9)).otherwise(pl.col("open")).alias("open")
    )
    with caplog.at_level("WARNING"):
        merged = mod._merge_raw(primary, fb_single)
    assert merged.height == 84, "冲突日仍保留, 不产生 coverage 缺口"
    assert bad_day in merged["date"].to_list()
    row = merged.filter(pl.col("date") == bad_day)
    assert row["open"][0] == 10.0, "冲突日取主源口径"
    assert row["source"][0] == "sina_hk_daily"
    assert any("取主源口径" in r.message for r in caplog.records)


def test_merge_raw_major_conflict_still_raises():
    """大量冲突 (远超 5%) → 仍抛错拒合并, 源坏了必须停下。"""
    days = [date(2026, 5, 1) + timedelta(days=i) for i in range(10)]
    primary = _raw_frame(days)
    fb = _raw_frame(days, open_=99.0, source="tencent_hk_daily")  # 全部行都冲突
    with pytest.raises(ValueError, match="raw_source_conflict"):
        mod._merge_raw(primary, fb)


def test_as_plain_date_normalizes_datetime():
    """datetime 压成 date —— 该坑已在 regime_builder/verify/merge 闸门各踩一次。"""
    stamp = datetime(2026, 9, 4, 15, 30)
    assert mod._as_plain_date(stamp) == date(2026, 9, 4)
    assert not isinstance(mod._as_plain_date(stamp), datetime)
    plain = date(2026, 9, 4)
    assert mod._as_plain_date(plain) is plain
