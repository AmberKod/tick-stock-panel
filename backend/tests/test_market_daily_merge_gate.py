"""merge_market_daily_frames 的 legacy 替换闸门测试 (09-15 类型不匹配 bug 回归)。

背景: 2792 只港股 legacy 分区的 date 列是 Datetime(us), provider 新数据是 Date。
Python 集合里 datetime 与 date 永不相等 (哈希不同), 差集恒为全部旧日期 →
「必须取得完整维护窗口」100% 误拒。修复: 比较前用 _as_plain_date 归一化两侧。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.tickflow.market_daily import merge_market_daily_frames

VERIFIED_COLS = {
    "price_schema_version": 1,
    "raw_price_verified": True,
    "price_adjustment": "unadjusted",
    "volume_unit": "share",
    "currency": "HKD",
}


def _verified_frame(dates: list, dates_as: str = "date") -> pl.DataFrame:
    n = len(dates)
    data = {
        "symbol": ["00005.HK"] * n,
        "date": dates,
        "close": [1.0] * n,
    }
    df = pl.DataFrame(data)
    if dates_as == "datetime":
        df = df.with_columns(pl.col("date").cast(pl.Datetime("us")))
    return df.with_columns(
        *[pl.lit(v).alias(k) for k, v in VERIFIED_COLS.items()]
    )


def _legacy_frame(dates: list, dates_as: str = "datetime") -> pl.DataFrame:
    n = len(dates)
    df = pl.DataFrame({
        "symbol": ["00005.HK"] * n,
        "date": dates,
        "close": [1.0] * n,
    })
    if dates_as == "datetime":
        df = df.with_columns(pl.col("date").cast(pl.Datetime("us")))
    return df


def test_legacy_datetime_fully_covered_by_date_incoming_passes():
    """Datetime legacy + Date incoming 全覆盖 → 必须放行 (09-15 修复点)。"""
    dates = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
    old = _legacy_frame(dates, dates_as="datetime")
    incoming = _verified_frame(dates, dates_as="date")

    merged = merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)

    assert merged.height == 3
    # 替换后应以 incoming (verified) 的行为准
    assert "price_schema_version" in merged.columns


def test_genuine_missing_day_still_rejected():
    """真缺失 (incoming 少一天) → 仍然拒绝, 零缺口原则不变。"""
    old = _legacy_frame([date(2026, 9, 1), date(2026, 9, 2)], dates_as="datetime")
    incoming = _verified_frame([date(2026, 9, 2)], dates_as="date")

    with pytest.raises(ValueError, match="完整维护窗口"):
        merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)


def test_few_stale_junk_days_tolerated():
    """远期垃圾日 (假日行/源坏行) 容忍替换。

    09-16 实证 00005.HK: 旧分区 6963 天含 2009-01-01 (元旦休市假日行) 等
    3 个新浪永远提供不了的日期, 完整覆盖不可能, 2794 只全卡死在此。
    """
    from datetime import timedelta

    junk = {date(2000, 3, 28), date(2009, 1, 1), date(2010, 4, 30)}
    # 600 个正常交易日, 从 1998 一直铺到 2026 (每 17 天一个, 避开垃圾日)
    old_days = [d for i in range(600)
                if (d := date(1998, 6, 1) + timedelta(days=17 * i)) not in junk]
    old = _legacy_frame(sorted(old_days) + sorted(junk), dates_as="datetime")
    # incoming 覆盖全部正常日期并推进到最新, 只缺 3 个垃圾日
    incoming = _verified_frame(sorted(old_days), dates_as="date")

    merged = merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)

    assert merged.height == len(old_days)
    merged_days = {d.date() if isinstance(d, datetime) else d for d in merged["date"].to_list()}
    assert not (junk & merged_days), "垃圾日不应被带入新分区"


def _legacy_and_incoming_with_loss(total: int, drop_ratio: float):
    """构造 legacy 分区, 其中 drop_ratio 比例的**远期**日期新浪不提供。"""
    from datetime import timedelta

    all_days = sorted(date(1998, 6, 1) + timedelta(days=17 * i) for i in range(total))
    drop_n = int(total * drop_ratio)
    # 从最早的一段开始丢 (远离尾端 90 天, 模拟停牌/冷门股旧源补行)
    dropped = set(all_days[:drop_n])
    kept = [d for d in all_days if d not in dropped]
    old = _legacy_frame(all_days, dates_as="datetime")
    incoming = _verified_frame(kept, dates_as="date")
    return old, incoming, dropped


def test_bulk_historical_loss_tolerated_up_to_threshold():
    """legacy 结构性不可靠: 旧分区把无成交日也填行, 丢失 10% 应放行。

    09-16 实测 36 只: 00026.HK 519/5244 (9.9%)、00021.HK 407/3567 (11.4%)。
    """
    old, incoming, dropped = _legacy_and_incoming_with_loss(600, 0.10)

    merged = merge_market_daily_frames(old, incoming, "00026.HK", replace_legacy=True)

    assert merged.height == 600 - len(dropped)


def test_excessive_historical_loss_rejected():
    """丢失超过 15% → 拒绝 (真抓错标的 / 拉取截断的信号)。"""
    old, incoming, _ = _legacy_and_incoming_with_loss(600, 0.25)

    with pytest.raises(ValueError, match="完整维护窗口"):
        merge_market_daily_frames(old, incoming, "00026.HK", replace_legacy=True)


def test_recent_missing_day_rejected_even_if_few():
    """缺的是尾端 90 天内的近期日期 → 拒绝 (数据源退化丢近期历史)。"""
    from datetime import timedelta

    last = date.today() - timedelta(days=3)
    old_days = [last - timedelta(days=7 * i) for i in range(19, -1, -1)]
    old = _legacy_frame(old_days, dates_as="datetime")
    incoming = _verified_frame(old_days[:-1], dates_as="date")  # 缺最新一天

    with pytest.raises(ValueError, match="完整维护窗口"):
        merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)


def test_staler_incoming_rejected_even_with_tolerable_gaps():
    """新数据没推进到旧分区最新日期 (更陈旧) → 拒绝, 防用旧数据替换新数据。"""
    from datetime import timedelta

    old_days = [date(2025, 1, 1) + timedelta(days=7 * i) for i in range(60)]
    old = _legacy_frame(old_days, dates_as="datetime")
    # incoming 缺 1 个远期垃圾日, 且最晚只到 2026-06 附近 (早于旧分区尾端)
    incoming_days = [d for d in old_days[:-1]][:-2]
    incoming = _verified_frame(incoming_days, dates_as="date")

    with pytest.raises(ValueError, match="完整维护窗口"):
        merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)


def test_date_legacy_uncovered_mixed_types_equivalent():
    """Datetime incoming 补 Date legacy (反向混型) 同样归一化正确。"""
    dates = [date(2026, 9, 3)]
    old = _legacy_frame(dates, dates_as="date")
    incoming = _verified_frame([datetime(2026, 9, 3, 0, 0)], dates_as="datetime")

    merged = merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=True)
    assert merged.height == 1


def test_replace_legacy_false_still_rejected():
    """replace_legacy=False → 拒绝路径不受归一化影响。"""
    old = _legacy_frame([date(2026, 9, 1)], dates_as="datetime")
    incoming = _verified_frame([date(2026, 9, 1)], dates_as="date")

    with pytest.raises(ValueError):
        merge_market_daily_frames(old, incoming, "00005.HK", replace_legacy=False)


def _frames_with_volume(old_rows: list[tuple[date, float]], incoming_rows: list[tuple[date, float]]):
    """(date, volume) 对构造 legacy / verified 帧, 用于无成交占位行场景。"""
    old = pl.DataFrame({
        "symbol": ["00007.HK"] * len(old_rows),
        "date": [d for d, _ in old_rows],
        "close": [0.032] * len(old_rows),
        "volume": [v for _, v in old_rows],
    }).with_columns(pl.col("date").cast(pl.Datetime("us")))
    incoming = pl.DataFrame({
        "symbol": ["00007.HK"] * len(incoming_rows),
        "date": [d for d, _ in incoming_rows],
        "close": [0.032] * len(incoming_rows),
        "volume": [v for _, v in incoming_rows],
    }).with_columns(*[pl.lit(v).alias(k) for k, v in VERIFIED_COLS.items()])
    return old, incoming


def test_zero_volume_placeholder_tail_does_not_block_repair():
    """停牌股的合成补行尾巴不参与"必须覆盖"日期集, 不得阻断修复。

    09-16 实证 00007.HK: 新浪真实数据止于 2024-03-28 (长期停牌), 旧分区却有
    245 行 volume=0 的合成补行一路铺到 2026-09-03, 被当作"旧分区最新日期" →
    第 3 条护栏拒绝整个修复。剔除无成交行后, 尾部边界回到真实最后交易日。
    """
    real = [(date(2024, 3, 27), 13_150_000.0), (date(2024, 3, 28), 7_090_000.0)]
    placeholders = [(date(2026, 6, 1), 0.0), (date(2026, 9, 3), 0.0)]
    old, incoming = _frames_with_volume(real + placeholders, real)

    merged = merge_market_daily_frames(old, incoming, "00007.HK", replace_legacy=True)

    assert merged.height == 2
    merged_days = {d.date() if isinstance(d, datetime) else d for d in merged["date"].to_list()}
    assert date(2026, 9, 3) not in merged_days, "合成补行不应被带入新分区"


def test_real_recent_rows_with_volume_still_protected():
    """真实成交行 (volume>0) 仍受第 3 条护栏保护, 不允许被更陈旧数据替换。"""
    from datetime import timedelta

    last = date.today() - timedelta(days=3)
    old_rows = [(last - timedelta(days=7 * i), 1000.0) for i in range(19, -1, -1)]
    old, incoming = _frames_with_volume(old_rows, old_rows[:-1])

    with pytest.raises(ValueError, match="完整维护窗口"):
        merge_market_daily_frames(old, incoming, "00007.HK", replace_legacy=True)


def test_today_dated_row_does_not_block_repair():
    """旧分区含今日真实行、而新源今日尚未发布 → 不应阻断 (09-16 实测抖动)。

    09-16 实证: 同一批 40 只, 20:37 新浪带当日行 (36/40 ok), 20:49 又回退掉
    (18/40 failed)。旧分区里那条当日真实行会把 guard2/guard3 同时点亮。
    """
    from datetime import timedelta

    today = date.today()
    confirmed = [(today - timedelta(days=1), 1000.0), (today - timedelta(days=2), 900.0)]
    old, incoming = _frames_with_volume([*confirmed, (today, 800.0)], confirmed)

    merged = merge_market_daily_frames(old, incoming, "00004.HK", replace_legacy=True)

    assert merged.height == 2
    days = {d.date() if isinstance(d, datetime) else d for d in merged["date"].to_list()}
    assert today not in days, "未定盘的当日行不应写回"


def test_us_symbol_bypasses_gate():
    """美股不走 HK 闸门, 混型日期直接 concat (原行为)。"""
    old = pl.DataFrame({
        "symbol": ["AAPL.US"], "date": [datetime(2026, 9, 1)], "close": [1.0],
    })
    incoming = pl.DataFrame({
        "symbol": ["AAPL.US"], "date": [datetime(2026, 9, 1)], "close": [2.0],
    })
    merged = merge_market_daily_frames(old, incoming, "AAPL.US", replace_legacy=True)
    assert merged.height == 1
