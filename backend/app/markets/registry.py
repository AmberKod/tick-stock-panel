"""市场档案注册表: market → profile, symbol → market。

M0 注册 CN; M1 注册 HK; M2 注册 US —— 三个市场**均已注册**,
profile_for_symbol 对三个市场都返回档案。get_profile 对未注册市场仍显式
KeyError, 防止新市场标的静默套用别家规则 (M0 护栏在 M2 之后依然有效)。
"""
from __future__ import annotations

from datetime import date, timedelta

from app.markets.cn import CN_PROFILE
from app.markets.hk import HK_PROFILE
from app.markets.profile import MarketProfile
from app.markets.us import US_PROFILE

_PROFILES: dict[str, MarketProfile] = {
    "CN": CN_PROFILE,
    "HK": HK_PROFILE,
    "US": US_PROFILE,
}

# 后缀 → 市场。M0 登记所有映射, M1/M2 持续生效。
_SUFFIX_TO_MARKET = {
    ".SH": "CN",
    ".SZ": "CN",
    ".BJ": "CN",
    ".HK": "HK",
    ".US": "US",
}


def get_profile(market: str = "CN") -> MarketProfile:
    """按市场 id 取档案; 未注册市场显式报错而不是静默回退 CN。"""
    key = (market or "CN").strip().upper()
    profile = _PROFILES.get(key)
    if profile is None:
        registered = ", ".join(sorted(_PROFILES))
        raise KeyError(
            f"Market profile not registered: {market!r} (registered: {registered})"
        )
    return profile


def resolve_market(symbol: str) -> str:
    """按 symbol 后缀解析市场。

    '00700.HK' → 'HK'; 'AAPL.US' → 'US';
    '600519.SH' / '600519' (无后缀) → 'CN'。
    """
    value = str(symbol or "").strip().upper()
    for suffix, market in _SUFFIX_TO_MARKET.items():
        if value.endswith(suffix):
            return market
    return "CN"


def profile_for_symbol(symbol: str) -> MarketProfile:
    """按 symbol 解析市场档案。

    CN / HK / US 均已注册 (M0→M2), 因此本函数对带 .SH/.SZ/.BJ/.HK/.US 后缀的
    symbol 都能返回档案; 未注册市场才会由 get_profile 抛 KeyError。
    无后缀 symbol 按 resolve_market 兜底为 CN。
    """
    return get_profile(resolve_market(symbol))


def cross_market_today() -> date:
    """跨市场「今日」= 所有已注册市场当日日期的**最大值**。

    用途: 需要一次性覆盖多个市场、又没有单一 symbol 上下文的地方
    (典型: 盘后管道的窗口右端)。取最大值即"最靠前"的那个市场, 作为窗口
    右端只会多覆盖, 绝不会截断任一市场的当日数据。

    必须遍历 _PROFILES 的 key 并逐个 get_profile(m).today(), **不得**退化成
    CN_PROFILE.today() / cn_today() 之类的常量引用: 那样等于隐含假设
    "北京 (UTC+8) 是全部已注册市场里日期最靠前的", 一旦注册 UTC+9 以东市场
    (JP/AU/NZ) 该假设失效, 这些市场的当日K会被静默漏掉。
    """
    return max(get_profile(m).today() for m in _PROFILES)


def cross_market_window(span_days: int) -> tuple[date, date]:
    """跨市场窗口 = (最落后市场的 today - span_days, 最靠前市场的 today)。

    左端取最小值以保证连最落后的市场也覆盖到 span_days 天; 右端取最大值以
    保证最靠前的市场当日数据不被截断。与 cross_market_today() 同源: 遍历
    _PROFILES, 不引用任何单一市场常量。

    生产调用方: jobs.daily_pipeline._run_market_daily_scheduled 的港美日 K
    增量同步窗口 (span_days=365)。窗口是**超集**语义 —— 两端都比任一单一市场
    更宽, 所以对某个具体市场而言只会多读, 不会截断。
    """
    todays = [get_profile(m).today() for m in _PROFILES]
    return min(todays) - timedelta(days=span_days), max(todays)
