"""市场档案注册表: market → profile, symbol → market。

M0 注册 CN; M1 注册 HK; M2 注册 US。
profile_for_symbol 对未注册市场显式 KeyError,
防止港美股标的静默套用 A 股规则 (H1 之前是 M0 护栏)。
"""
from __future__ import annotations

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

    M1 起 HK 已注册; US 仍抛 KeyError (M2 落地前保留护栏)。
    """
    return get_profile(resolve_market(symbol))
