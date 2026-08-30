"""Provider registry.

[M0] 新增市场维度路由: 同名 provider 按市场注册, symbol 侧入口
resolve_provider_for_symbol 供 M1 港股/M2 美股接入。M0 阶段仅
CN → tickflow 一条链路, 默认调用路径行为与历史完全一致。
"""
from __future__ import annotations

from app.data_providers.base import MarketDataProvider
from app.data_providers.tickflow_provider import TickFlowProvider
from app.markets.registry import resolve_market

# market → provider 注册表 (M0: 仅 A 股 tickflow 独占;
# M1: "HK" → quickquote/akshare; M2: "US" → yfinance)
_MARKET_PROVIDERS: dict[str, dict[str, type]] = {
    "CN": {"tickflow": TickFlowProvider},
}

# 兼容旧入口: 无市场维度的按名取 provider (默认 CN)
_PROVIDERS = _MARKET_PROVIDERS["CN"]

# 各市场默认数据源 (get_provider 未指定 name 时使用)
_MARKET_DEFAULT_PROVIDER = {"CN": "tickflow"}


def get_provider(name: str = "tickflow", market: str = "CN") -> MarketDataProvider:
    """按名称+市场取 provider 实例。

    默认参数 (tickflow/CN) 与历史行为完全一致; 既有调用方零改动。
    """
    key = (name or "tickflow").lower()
    market_key = (market or "CN").strip().upper()
    providers = _MARKET_PROVIDERS.get(market_key)
    if providers is None:
        registered = ", ".join(sorted(_MARKET_PROVIDERS))
        raise ValueError(f"Unsupported market: {market} (registered: {registered})")
    provider_cls = providers.get(key)
    if provider_cls is None:
        raise ValueError(f"Unsupported data provider: {name} for market {market_key}")
    return provider_cls()


def resolve_provider_for_symbol(symbol: str) -> MarketDataProvider:
    """按 symbol 的市场后缀路由到对应 provider。

    M0 护栏: 非 CN 市场 (HK/US) 显式 NotImplementedError,
    防止港美股标的静默走 A 股数据链路。M1/M2 注册后自然放开。
    """
    market = resolve_market(symbol)
    if market not in _MARKET_PROVIDERS:
        raise NotImplementedError(
            f"Market {market} not yet available (symbol={symbol!r}); "
            "M1: HK, M2: US. See app/markets/registry.py"
        )
    return get_provider(_MARKET_DEFAULT_PROVIDER[market], market=market)
