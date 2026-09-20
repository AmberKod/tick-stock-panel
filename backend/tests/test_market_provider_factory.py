"""市场 Provider 工厂的配置边界测试。"""
from __future__ import annotations

from unittest.mock import patch

from app.data_providers.fallback_provider import RealtimeFallbackProvider
from app.data_providers.hk_quickquote_provider import HKQuickQuoteProvider
from app.data_providers.market_provider_factory import create_market_realtime_provider
from app.data_providers.yfinance_provider import YFinanceProvider


def test_us_fallback_is_disabled_by_default():
    with patch("app.data_providers.market_provider_factory.settings.market_quote_fallback_enabled", False):
        provider = create_market_realtime_provider("us")
    try:
        assert isinstance(provider, YFinanceProvider)
        assert not isinstance(provider, RealtimeFallbackProvider)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def test_us_fallback_is_opt_in():
    with patch("app.data_providers.market_provider_factory.settings.market_quote_fallback_enabled", True), patch(
        "app.data_providers.market_provider_factory.settings.market_quote_fallback_timeout_seconds", 1.5
    ):
        provider = create_market_realtime_provider("us")
    try:
        assert isinstance(provider, RealtimeFallbackProvider)
        assert provider.primary.name == "yfinance"
        assert provider.secondary.name == "tencent_us_candidate"
        assert provider.policy.enabled is True
        assert provider.policy.request_timeout_seconds == 1.5
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def test_hk_uses_existing_provider_chain():
    provider = create_market_realtime_provider("hk")
    try:
        assert isinstance(provider, HKQuickQuoteProvider)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
