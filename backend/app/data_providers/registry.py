"""Provider registry.

[M0] 新增市场维度路由: 同名 provider 按市场注册, symbol 侧入口
resolve_provider_for_symbol 供 M1 港股/M2 美股接入。
[M1] HK → hk_quickquote (腾讯/新浪 HTTP) 已注册。
"""
from __future__ import annotations

from app.data_providers.base import MarketDataProvider
from app.data_providers.hk_daily_provider import HKDailyProvider
from app.data_providers.hk_quickquote_provider import HKQuickQuoteProvider
from app.data_providers.sina_us_provider import SinaUSProvider
from app.data_providers.tickflow_provider import TickFlowProvider
from app.data_providers.yfinance_provider import YFinanceProvider
from app.markets.registry import resolve_market

# market → provider 注册表
# CN: tickflow 独占 (含 tick 级能力)
# HK: hk_quickquote 走腾讯/美股 HTTP (日级 + 实时; 分钟/tick 不可用)
# US: sina (新浪日K主源, yfinance 兜底在适配层) + yfinance (日K/实时兜底)
_MARKET_PROVIDERS: dict[str, dict[str, type]] = {
    "CN": {"tickflow": TickFlowProvider},
    "HK": {"hk_quickquote": HKQuickQuoteProvider, "hk_daily": HKDailyProvider},
    "US": {"yfinance": YFinanceProvider, "sina": SinaUSProvider},
}

# 兼容旧入口: 无市场维度的按名取 provider (默认 CN)
_PROVIDERS = _MARKET_PROVIDERS["CN"]

# 各市场默认数据源 (get_provider 未指定 name 时使用)
# US 默认 sina (2026-09-22 数据地基批 #5): yfinance 的 Yahoo 端点已持续 403,
# 且 403 不触发原熔断 (只认 429 系) → 循环空烧后静默空 df。新浪为免费美股
# 日 K 主源, 默认源切它; yfinance 保留注册作显式兜底。
_MARKET_DEFAULT_PROVIDER = {
    "CN": "tickflow",
    "HK": "hk_quickquote",
    "US": "sina",
}


def get_provider(name: str = "tickflow", market: str = "CN") -> MarketDataProvider:
    """按名称+市场取 provider 实例。

    默认参数 (tickflow/CN) 与历史行为完全一致; 既有调用方零改动。
    """
    key = (name or "tickflow").lower()
    market_key = (market or "CN").strip().upper()
    if market_key == "HK" and key == "hk_financial":
        from app.data_providers.hk_financial_provider import HKFinancialProvider

        return HKFinancialProvider()
    providers = _MARKET_PROVIDERS.get(market_key)
    if providers is None:
        registered = ", ".join(sorted(_MARKET_PROVIDERS))
        raise ValueError(f"Unsupported market: {market} (registered: {registered})")
    provider_cls = providers.get(key)
    if provider_cls is None:
        raise ValueError(f"Unsupported data provider: {name} for market {market_key}")
    return provider_cls()


def get_default_provider(market: str, *, dataset: str | None = None) -> MarketDataProvider:
    """Resolve the registered default without changing global preferences."""
    market_key = str(market).strip().upper()
    if market_key not in _MARKET_DEFAULT_PROVIDER:
        raise ValueError(f"Unsupported market: {market_key}")
    if market_key == "HK" and dataset in {"daily", "adj_factor", "financial"}:
        return get_provider("hk_financial" if dataset == "financial" else "hk_daily", market="HK")
    return get_provider(_MARKET_DEFAULT_PROVIDER[market_key], market=market_key)


def resolve_provider_for_symbol(symbol: str) -> MarketDataProvider:
    """按 symbol 的市场后缀路由到对应 provider。

    M0 护栏已就位: 非注册市场显式 NotImplementedError,
    防止港美股标的静默走 A 股数据链路。M2 之后三个市场全部注册。
    """
    market = resolve_market(symbol)
    if market not in _MARKET_PROVIDERS:
        raise NotImplementedError(
            f"Market {market} not yet available (symbol={symbol!r}); "
            f"registered: {sorted(_MARKET_PROVIDERS)}. See app/markets/registry.py"
        )
    return get_default_provider(market)
