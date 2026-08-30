"""美股档案 (US_PROFILE) 单测: DST 时钟 + 无涨跌停 + 字母代码路由。

M2 阶段: 不依赖 yfinance, 仅验证档案本身行为正确。
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.markets import profile_for_symbol
from app.markets.cn import CN_PROFILE
from app.markets.us import US_PROFILE, US_TZ

US = US_PROFILE


# ── 协议契约 ──

def test_us_basic_attributes():
    assert US.market == "US"
    assert US.tz_name == "America/New_York"
    assert US.currency == "USD"
    assert US.settlement == "T+0"
    assert US.lot_size is None
    assert US.symbol_suffixes == (".US",)
    assert US.trading_minutes_total == 390.0
    assert len(US.sessions) == 1  # 单一时段无午休


def test_us_core_indices():
    symbols = [r.symbol for r in US.core_indices]
    assert symbols == ["^GSPC.US", "^IXIC.US", "^DJI.US"]
    assert {r.symbol: r.name for r in US.core_indices}["^GSPC.US"] == "标普500"


def test_us_no_price_limit():
    assert US.has_price_limit() is False
    assert US.limit_pct("AAPL.US", datetime.now(US_TZ).date()) is None

def test_us_benchmarks_empty():
    assert US.bench_rt_candidates == ()
    assert US.benchmark_fallbacks == {}


# ── 时钟: DST 自动切换 ──

def test_us_dst_switch():
    """DST: 1月 EST = UTC-5, 7月 EDT = UTC-4。"""
    from datetime import date
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    jul = datetime(2026, 7, 15, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert jan.utcoffset().total_seconds() == -5 * 3600    # EST
    assert jul.utcoffset().total_seconds() == -4 * 3600     # EDT


def test_trading_minutes_session():
    """9:30-16:00 (0~390) 单一时段, 无午休。"""
    cases = [
        (datetime(2026, 8, 28, 9, 0), 0.0),      # 开盘前
        (datetime(2026, 8, 28, 9, 30), 0.0),     # 恰开盘
        (datetime(2026, 8, 28, 10, 30), 60.0),   # 早盘 1 小时
        (datetime(2026, 8, 28, 12, 0), 150.0),   # 中午 (投资时间 150 min, 无午休)
        (datetime(2026, 8, 28, 16, 0), 390.0),   # 收盘
        (datetime(2026, 8, 28, 17, 0), 390.0),   # 盘后
    ]
    for dt, expected in cases:
        assert US.trading_minutes_elapsed_from_dt(dt) == expected, dt


def test_trading_minutes_random_in_range():
    rng = random.Random(20260830)
    for _ in range(500):
        d = datetime(2026, 8, 28, rng.randrange(0, 24), rng.randrange(0, 60))
        v = US.trading_minutes_elapsed_from_dt(d)
        assert 0.0 <= v <= 390.0, (d, v)


def test_trading_minutes_from_ts_invalid():
    for bad in [None, 0, "bad"]:
        assert US.trading_minutes_elapsed_from_ts(bad) == 390.0


# ── 代码路由 ──

def test_fallback_suffix_us():
    assert US.fallback_suffix("AAPL") == ".US"
    assert US.fallback_suffix("MSFT") == ".US"
    assert US.fallback_suffix("00700") == ""   # 数字不接受
    assert US.fallback_suffix("600519") == "" # 6 位数字不接受


# ── 注册表 ──

def test_resolve_market_us():
    from app.markets import resolve_market
    assert resolve_market("AAPL.US") == "US"
    assert resolve_market("MSFT.US") == "US"
    assert resolve_market("") == "CN"  # 无后缀默认 CN


def test_profile_for_symbol_us():
    assert profile_for_symbol("AAPL.US") is US_PROFILE
    assert profile_for_symbol("600519.SH") is CN_PROFILE


# ── 时间 ──

def test_now_and_today():
    now = US.now()
    assert now.tzinfo is not None
    # 美股 DST 期间 offset 是 -4 (7月) 或 -5 (1月), 都是负数
    assert now.utcoffset().total_seconds() in (-5*3600, -4*3600)
    assert US.today() == now.date()