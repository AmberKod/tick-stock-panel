"""按市场创建行情 Provider，集中管理可选的外部回退源。"""
from __future__ import annotations

from typing import Literal

from app.config import settings
from app.data_providers.fallback_provider import FallbackPolicy, RealtimeFallbackProvider
from app.data_providers.hk_quickquote_provider import HKQuickQuoteProvider
from app.data_providers.tencent_market_provider import TencentMultiMarketProvider
from app.data_providers.yfinance_provider import YFinanceProvider

Market = Literal["hk", "us"]


def create_market_realtime_provider(market: Market):
    """创建港美股实时 Provider。

    港股现有 HKQuickQuoteProvider 已内置腾讯→新浪降级，因此不再叠加候选
    腾讯源；美股默认使用 yfinance，只有显式开启开关才增加腾讯候选回退。
    """
    if market == "hk":
        return HKQuickQuoteProvider()

    primary = YFinanceProvider()
    if not settings.market_quote_fallback_enabled:
        return primary

    secondary = TencentMultiMarketProvider("us", timeout=settings.market_quote_fallback_timeout_seconds)
    return RealtimeFallbackProvider(
        primary,
        secondary,
        policy=FallbackPolicy(
            enabled=True,
            cache_ttl_seconds=settings.market_quote_fallback_cache_ttl_seconds,
            request_timeout_seconds=settings.market_quote_fallback_timeout_seconds,
        ),
    )
