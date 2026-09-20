"""港股档案 (HK_PROFILE) 单测: 时钟边界 + 涨跌停 None + 5位代码兜底。

M1 阶段: 不依赖 akshare/httpx, 仅验证档案本身的行为正确性。
Provider 单测见 test_hk_quickquote_provider.py (mock httpx)。
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from app.markets import profile_for_symbol, resolve_market
from app.markets.cn import CN_PROFILE
from app.markets.hk import HK_PROFILE

# 注: HK_PROFILE 在 M1 H1 已就位, 但 registry 注册在 H2 完成。
# 本测试不依赖 get_profile, 直接使用单例, 保证 H1/H2 可独立 revert。
HK = HK_PROFILE


# ── 协议契约 ──────────────────────────────────────

def test_hk_basic_attributes():
    assert HK.market == "HK"
    assert HK.tz_name == "Asia/Hong_Kong"
    assert HK.currency == "HKD"
    assert HK.settlement == "T+2"
    assert HK.same_day_sell_allowed is True
    assert HK.lot_size is None
    assert HK.symbol_suffixes == (".HK",)
    assert HK.trading_minutes_total == 330.0
    assert len(HK.sessions) == 2


def test_hk_core_indices():
    symbols = [r.symbol for r in HK.core_indices]
    assert symbols == ["HSI.HK", "HSTECH.HK", "HSCEI.HK"]
    assert {r.symbol: r.name for r in HK.core_indices}["HSI.HK"] == "恒生指数"


def test_hk_no_price_limit():
    assert HK.has_price_limit() is False
    assert HK.limit_pct("00700.HK", date(2026, 8, 28)) is None
    assert HK.limit_pct("00700.HK", date(2026, 8, 28), is_risk_warning=True) is None
    # 与 CN 对照: CN 返回 0.10, HK 返回 None
    assert CN_PROFILE.limit_pct("600519.SH", date(2026, 8, 28)) == 0.10


def test_hk_benchmarks_empty():
    """港股无 ST/无涨跌停, bench 候选留空。"""
    assert HK.bench_rt_candidates == ()
    assert HK.benchmark_fallbacks == {}


# ── 时钟 ──────────────────────────────────────────

def test_trading_minutes_session_boundaries():
    """港股档期: 9:30-12:00 (0~150) + 13:00-16:00 (150~330)。"""
    day = date(2026, 8, 28)
    cases = [
        (datetime(day.year, day.month, day.day, 9, 0), 0.0),       # 开盘前
        (datetime(day.year, day.month, day.day, 9, 30, 0), 0.0),    # 恰开盘
        (datetime(day.year, day.month, day.day, 10, 30), 60.0),    # 早市 1 小时
        (datetime(day.year, day.month, day.day, 12, 0), 150.0),    # 早市结束
        (datetime(day.year, day.month, day.day, 12, 30), 150.0),   # 午休保持
        (datetime(day.year, day.month, day.day, 13, 0, 0), 150.0), # 午市开盘
        (datetime(day.year, day.month, day.day, 15, 0), 270.0),    # 午市 2 小时
        (datetime(day.year, day.month, day.day, 16, 0), 330.0),    # 收盘
        (datetime(day.year, day.month, day.day, 23, 59), 330.0),   # 盘后
    ]
    for dt, expected in cases:
        assert HK.trading_minutes_elapsed_from_dt(dt) == expected, dt


def test_trading_minutes_random_sampling():
    """1000 点随机采样, 验证结果在 [0, 330] 闭区间内。"""
    rng = random.Random(20260830)
    for _ in range(1000):
        day = date(2026, 1, 1) + timedelta(days=rng.randrange(0, 400))
        minute = rng.randrange(0, 24 * 60)
        second = rng.randrange(0, 60)
        dt = datetime(day.year, day.month, day.day, minute // 60, minute % 60, second)
        result = HK.trading_minutes_elapsed_from_dt(dt)
        assert 0.0 <= result <= 330.0, (dt, result)


def test_trading_minutes_from_ts_invalid():
    """timestamp 缺失/明显无效 → 返回 330 (全天兜底, 避免量比被折算成 0)。

    注意: 1.5 经 int()=1 后是合法 Unix 时间戳 (1970-01-01 08:00:01),
    不应假设其结果为 330; 边界值只测 None/"bad"/0。
    """
    for bad in [None, 0, "bad"]:
        assert HK.trading_minutes_elapsed_from_ts(bad) == 330.0


def test_trading_minutes_from_ts_valid_range():
    """合法时间戳 → 结果在 [0, 330] 闭区间内。"""
    samples = [1_700_000_000_000, 1_750_000_000_000, 1_800_000_000_000]  # 2023-11, 2025-06, 2026-11
    for ts in samples:
        result = HK.trading_minutes_elapsed_from_ts(ts)
        assert 0.0 <= result <= 330.0, ts


# ── 代码路由 ──────────────────────────────────────

def test_fallback_suffix_hk():
    assert HK.fallback_suffix("00700") == ".HK"
    assert HK.fallback_suffix("09988") == ".HK"
    assert HK.fallback_suffix("00005") == ".HK"
    # 不接受 6 位数字 (那是 A 股)
    assert HK.fallback_suffix("600519") == ""
    assert HK.fallback_suffix("abc123") == ""


# ── 注册表解析 ────────────────────────────────────

def test_resolve_market_hk():
    assert resolve_market("00700.HK") == "HK"
    assert resolve_market("HSI.HK") == "HK"
    assert resolve_market("hsi.hk") == "HK"  # 大小写归一


def test_profile_for_symbol_cn_unchanged():
    """M1 注册 HK 不应影响 CN 解析。

    H2 之前 HK 未注册到 registry, profile_for_symbol("00700.HK") 仍会抛 KeyError。
    此处仅断言 CN 路径行为不变。
    """
    assert profile_for_symbol("600519.SH") is CN_PROFILE
    assert profile_for_symbol("600519") is CN_PROFILE


# ── 时间无关 sanity ────────────────────────────────

def test_now_and_today():
    now = HK.now()
    today = HK.today()
    assert now.tzinfo is not None
    # HK 与北京同时区, 时差为 0
    assert now.utcoffset().total_seconds() == 8 * 3600
    assert today == now.date()


# ── H3b: ext_data.normalize_symbol 兜底加 HK 路径 ──

def test_normalize_symbol_5digit_to_hk():
    """M1 H3b: 5 位数字代码 → .HK 兜底 (不走 CN 的 6 位规则)。"""
    import polars as pl

    from app.services.ext_data import normalize_symbol
    r = normalize_symbol(pl.Series(["00700", "09988", "03690"]))
    assert r.to_list() == ["00700.HK", "09988.HK", "03690.HK"]


def test_normalize_symbol_mixed():
    """5 位 → .HK; 6 位 → CN 兜底; 含 . → 透传。"""
    import polars as pl

    from app.services.ext_data import normalize_symbol
    r = normalize_symbol(pl.Series([
        "00700",      # 5 位 → HK
        "600519",     # 6 位 → CN 兜底 .SH
        "000001",     # 6 位 → CN 兜底 .SZ
        "00700.HK",   # 已含后缀, 透传
        "AAPL.US",    # 已含后缀, 透传
    ]))
    assert r.to_list() == [
        "00700.HK", "600519.SH", "000001.SZ", "00700.HK", "AAPL.US",
    ]


def test_normalize_symbol_hk_fallback_ambiguity():
    """5 位数字 00005 / 00010 等小码 → .HK (无歧义, 因为 A 股 5xxxx 已退市不存在)。"""
    import polars as pl

    from app.services.ext_data import normalize_symbol
    r = normalize_symbol(pl.Series(["00005", "00005a", "80000"]))
    assert r.to_list() == ["00005.HK", "00005a", "80000.HK"]


# ── H4 软门控在指标管道的验证: 港股 00700.HK 计算涨跌停 → null ──

def test_polars_price_limit_pct_hk_returns_null():
    """H4 软门控在 polars 表达式层: 港股 00700.HK → null, A 股 → 10%。"""
    import polars as pl

    from app.price_limits import polars_price_limit_pct
    df = pl.DataFrame({
        "symbol": ["600519.SH", "00700.HK", "AAPL.US", "000001.SZ"],
        "date": pl.date_range(date(2026, 1, 1), date(2026, 1, 4), eager=True),
        "name": ["茅台", "腾讯", "苹果", "平安"],
    })
    df.with_columns(
        polars_price_limit_pct(pl.col("symbol"), pl.col("date"), pl.lit(False))
    )
    # 改列名为 limit_pct 输出
    result = polars_price_limit_pct(df["symbol"], df["date"], pl.lit(False))
    # 用 DataFrame 计算
    out = df.with_columns(result.alias("limit_pct"))
    # 600519.SH → 0.10, 00700.HK → null, AAPL.US → null, 000001.SZ → 0.10
    vals = dict(zip(out["symbol"].to_list(), out["limit_pct"].to_list(), strict=True))
    assert vals["600519.SH"] == 0.10
    assert vals["00700.HK"] is None
    assert vals["AAPL.US"] is None
    assert vals["000001.SZ"] == 0.10
