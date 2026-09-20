"""M0 对拍测试: app.markets 档案与旧 market_time/price_limits 输出等价。

本测试是"零行为变更"的数学证明 —— C1 阶段旧函数仍是本体,
C2 降级为 facade 转发后, 同样的对拍必须继续通过。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from app.market_time import (
    trading_minutes_elapsed_from_dt as legacy_minutes_dt,
)
from app.market_time import (
    trading_minutes_elapsed_from_ts as legacy_minutes_ts,
)
from app.markets import get_profile, profile_for_symbol, resolve_market
from app.markets.cn import CN_PROFILE
from app.markets.symbols import is_valid, normalize, parse
from app.price_limits import board_limit_pct as legacy_board_limit
from app.price_limits import price_limit_pct as legacy_limit_pct

CN = get_profile("CN")


def _random_dt(rng: random.Random) -> datetime:
    """随机生成覆盖全天各时段边界附近的北京时间 datetime。"""
    day = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 400))
    minute = rng.randrange(0, 24 * 60)
    second = rng.randrange(0, 60)
    return datetime(day.year, day.month, day.day, minute // 60, minute % 60, second)


def test_trading_minutes_random_sampling_equivalence():
    """5000 点随机采样对拍: profile 与旧实现输出完全一致。"""
    rng = random.Random(20260830)
    for _ in range(5000):
        dt = _random_dt(rng)
        assert CN.trading_minutes_elapsed_from_dt(dt) == legacy_minutes_dt(dt), dt


def test_trading_minutes_session_boundaries():
    """关键边界: 开盘前/早盘/午休/午后/收盘后。"""
    day = date(2026, 8, 28)
    cases = [
        (datetime(day.year, day.month, day.day, 9, 15), 0.0),      # 开盘前
        (datetime(day.year, day.month, day.day, 9, 30, 0), 0.0),   # 恰开盘
        (datetime(day.year, day.month, day.day, 10, 0), 30.0),     # 早盘
        (datetime(day.year, day.month, day.day, 11, 30), 120.0),   # 早盘结束
        (datetime(day.year, day.month, day.day, 12, 30), 120.0),   # 午休保持
        (datetime(day.year, day.month, day.day, 13, 0, 0), 120.0), # 午后开盘
        (datetime(day.year, day.month, day.day, 14, 0), 180.0),    # 午后
        (datetime(day.year, day.month, day.day, 15, 0), 240.0),    # 收盘
        (datetime(day.year, day.month, day.day, 23, 59), 240.0),   # 盘后
    ]
    for dt, expected in cases:
        assert CN.trading_minutes_elapsed_from_dt(dt) == expected, dt
        assert legacy_minutes_dt(dt) == expected, dt


def test_trading_minutes_from_ts_equivalence():
    rng = random.Random(42)
    samples = [None, 0, "bad", -1, 1.5, 1e12]
    samples += [rng.randrange(1_600_000_000_000, 1_800_000_000_000) for _ in range(2000)]
    for ts in samples:
        assert CN.trading_minutes_elapsed_from_ts(ts) == legacy_minutes_ts(ts), ts


def test_limit_pct_equivalence_all_boards():
    symbols = [
        "600519.SH",  # 沪主板
        "000001.SZ",  # 深主板
        "300750.SZ",  # 创业板
        "301123.SZ",  # 创业板注册制
        "688981.SH",  # 科创板
        "689009.SH",  # 科创板 CDR
        "832000.BJ",  # 北交所
        "430047.BJ",  # 北交所
    ]
    dates = [
        date(2024, 1, 1),
        date(2026, 7, 5),   # 制度切换前一日
        date(2026, 7, 6),   # 主板 ST 新规生效日
        date(2026, 12, 31),
    ]
    for sym in symbols:
        for d in dates:
            for st in (False, True):
                assert CN.limit_pct(sym, d, is_risk_warning=st) == legacy_limit_pct(
                    sym, d, is_risk_warning=st
                ), (sym, d, st)
                assert CN.board_limit_pct(sym) == legacy_board_limit(sym), sym


def test_limit_pct_st_only_before_change_date():
    """ST 5% 仅在 2026-07-06 之前的主板生效。"""
    assert CN.limit_pct("600519.SH", date(2026, 7, 5), is_risk_warning=True) == 0.05
    assert CN.limit_pct("600519.SH", date(2026, 7, 6), is_risk_warning=True) == 0.10
    # 创业板 ST 不适用 5% 规则
    assert CN.limit_pct("300750.SZ", date(2025, 1, 1), is_risk_warning=True) == 0.20


def test_resolve_market():
    assert resolve_market("600519.SH") == "CN"
    assert resolve_market("000001.SZ") == "CN"
    assert resolve_market("832000.BJ") == "CN"
    assert resolve_market("600519") == "CN"        # 无后缀默认 CN
    assert resolve_market("00700.HK") == "HK"
    assert resolve_market("AAPL.US") == "US"
    assert resolve_market("aapl.us") == "US"       # 大小写归一
    assert resolve_market("") == "CN"


def test_profile_for_symbol_guard():
    """M0→M2 护栏演进: HK (M1) / US (M2) 均已注册。

    三个市场解析正常, 无 KeyError (所有市场落地后可正常路由)。
    """
    from app.markets.hk import HK_PROFILE
    from app.markets.us import US_PROFILE
    assert profile_for_symbol("600519.SH") is CN_PROFILE
    assert profile_for_symbol("00700.HK") is HK_PROFILE
    assert profile_for_symbol("AAPL.US") is US_PROFILE


def test_core_indices_match_legacy_hardcode():
    """五处历史硬编码的同一四指数集合 (含 000680.SH 科创综指)。"""
    assert [r.symbol for r in CN.core_indices] == [
        "000001.SH", "399001.SZ", "399006.SZ", "000680.SH",
    ]
    assert {r.symbol: r.name for r in CN.core_indices}["000680.SH"] == "科创综指"


def test_symbols_parse_normalize():
    assert parse("600519.SH") == ("600519", ".SH")
    assert parse("00700.HK") == ("00700", ".HK")
    assert parse("600519") == ("600519", "")
    assert normalize("sh600519.sh ") == "sh600519.SH"  # 前缀剥离属 M1 容错范围
    assert normalize("aapl.us") == "aapl.US"
    assert is_valid("600519.SH")
    assert not is_valid("600519")      # 缺后缀
    assert not is_valid("60051.SH")    # A 股必须 6 位
    assert is_valid("AAPL.US")


def test_fallback_suffix():
    assert CN_PROFILE.fallback_suffix("600519") == ".SH"
    assert CN_PROFILE.fallback_suffix("000001") == ".SZ"
    assert CN_PROFILE.fallback_suffix("300750") == ".SZ"
