"""市场档案注册表: market → profile, symbol → market。

M0 仅注册 CN。HK/US 的后缀映射先行登记 (供 resolve_market 识别),
但 profile 本体在 M1/M2 落地 — profile_for_symbol 对未注册市场
显式 KeyError, 防止港美股标的静默套用 A 股规则。
"""
from __future__ import annotations

from app.markets.cn import CN_PROFILE
from app.markets.profile import MarketProfile

_PROFILES: dict[str, MarketProfile] = {
    "CN": CN_PROFILE,
}

# 后缀 → 市场。M0 只登记映射, HK/US 的 profile 待 M1/M2 注册。
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

    M0 阶段遇到 .HK/.US 会抛 KeyError —— 这是防止港美股数据
    误入 A 股链路的护栏, M1/M2 注册对应 profile 后自然放开。
    """
    return get_profile(resolve_market(symbol))
