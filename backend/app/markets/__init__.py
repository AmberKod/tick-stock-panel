"""市场抽象层 (M0): 时钟/交易制度/代码规则/指数基准的单一事实源。

用法:
    from app.markets import get_profile, profile_for_symbol, resolve_market

    cn = get_profile("CN")          # A 股档案
    cn.trading_minutes_total         # 240.0
    profile_for_symbol("00700.HK")   # M0: KeyError (M1 注册后放开)
"""
from app.markets.profile import IndexRef, MarketId, MarketProfile, TradingSession
from app.markets.registry import get_profile, profile_for_symbol, resolve_market
from app.markets.symbols import normalize, parse, is_valid

__all__ = [
    "IndexRef",
    "MarketId",
    "MarketProfile",
    "TradingSession",
    "get_profile",
    "profile_for_symbol",
    "resolve_market",
    "parse",
    "normalize",
    "is_valid",
]
