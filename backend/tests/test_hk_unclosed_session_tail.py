"""旧分区"未收盘当日占位行"剔除策略回归 (09-16 实证 00005.HK)。

背景: 腾讯备用源盘中会返回当日未完成 K 线, 曾落盘 (00005.HK 残留 2026-09-16,
成交量仅为前一日的 1/4)。腾讯随后被 WAF 全站 501, 新拉取只剩新浪 (止于已收盘的
前一日)。于是 merged.max 比内存复权快照 coverage_end 新一天,
`_hk_factors_for_window` 报"覆盖不足或版本冲突", 该标的永久卡死、无法自愈。

修复: 只剔除"晚于新拉取最新日期且不早于今日 (HK)"的占位行; 其他更近历史
(不可能是未收盘当日) 依旧 fail-closed, 防止静默丢失真实近期数据。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.markets.hk import HK_TZ
from app.services.hk_data_adapter import _drop_unclosed_session_tail

SYMBOL = "00005.HK"


def _frame(days: list[date]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [SYMBOL] * len(days),
        "date": days,
        "close": [1.0] * len(days),
    })


def test_no_tail_returns_unchanged():
    today = datetime.now(HK_TZ).date()
    merged = _frame([today - timedelta(days=2), today - timedelta(days=1)])
    out = _drop_unclosed_session_tail(SYMBOL, merged, today - timedelta(days=1))
    assert out.height == 2


def test_unclosed_session_placeholder_dropped():
    """新拉取止于前一日, 旧分区多出今日占位行 → 剔除。"""
    today = datetime.now(HK_TZ).date()
    yesterday = today - timedelta(days=1)
    merged = _frame([yesterday, today])
    out = _drop_unclosed_session_tail(SYMBOL, merged, yesterday)
    assert out["date"].to_list() == [yesterday]


def test_future_dated_placeholder_dropped():
    """旧分区残留今日之后的日期 (时区边界) 同样按占位行剔除。"""
    today = datetime.now(HK_TZ).date()
    merged = _frame([today - timedelta(days=1), today + timedelta(days=1)])
    out = _drop_unclosed_session_tail(SYMBOL, merged, today - timedelta(days=1))
    assert out["date"].to_list() == [today - timedelta(days=1)]


def test_older_missing_history_fails_closed():
    """旧分区含新拉取未覆盖的"今日之前"更近历史 → 拒绝发布 (可能丢真实数据)。"""
    today = datetime.now(HK_TZ).date()
    merged = _frame([today - timedelta(days=5), today - timedelta(days=2)])
    with pytest.raises(ValueError, match="未覆盖的更近历史日期"):
        _drop_unclosed_session_tail(SYMBOL, merged, today - timedelta(days=5))


def test_empty_frame_is_noop():
    out = _drop_unclosed_session_tail(SYMBOL, pl.DataFrame(), date(2026, 9, 15))
    assert out.is_empty()


def test_datetime_column_is_normalized_to_date():
    """legacy 分区的 Datetime(us) 列不能被 datetime/date 比较坑到 (第 6 次)。"""
    today = datetime.now(HK_TZ).date()
    yesterday = today - timedelta(days=1)
    merged = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL],
        "date": [datetime.combine(yesterday, datetime.min.time()), datetime.combine(today, datetime.min.time())],
        "close": [1.0, 2.0],
    }).with_columns(pl.col("date").cast(pl.Datetime("us")))
    out = _drop_unclosed_session_tail(SYMBOL, merged, yesterday)
    assert out["date"].to_list() == [yesterday]
    assert out.schema["date"] == pl.Date
