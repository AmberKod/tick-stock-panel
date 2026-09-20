"""可配置行情回退包装器的离线测试。"""
from __future__ import annotations

import time

import polars as pl
import pytest

from app.data_providers.base import ProviderCapabilities
from app.data_providers.fallback_provider import FallbackPolicy, RealtimeFallbackProvider


class FakeProvider:
    capabilities = ProviderCapabilities(realtime=True)

    def __init__(self, name: str, frames: list[pl.DataFrame | Exception]):
        self.name = name
        self.frames = list(frames)
        self.calls = 0

    def get_realtime(self, *, symbols: list[str]):
        self.calls += 1
        result = self.frames.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        return None


def quote(symbol: str, price: float) -> pl.DataFrame:
    return pl.DataFrame({"symbol": [symbol], "last_price": [price], "source": ["test"]})


def test_disabled_policy_never_calls_secondary():
    primary = FakeProvider("primary", [pl.DataFrame()])
    secondary = FakeProvider("secondary", [quote("AAPL.US", 200)])
    provider = RealtimeFallbackProvider(primary, secondary)

    result = provider.get_realtime(symbols=["AAPL.US"])

    assert result.is_empty()
    assert primary.calls == 1
    assert secondary.calls == 0


def test_enabled_policy_falls_back_on_empty_primary():
    primary = FakeProvider("primary", [pl.DataFrame()])
    secondary = FakeProvider("secondary", [quote("AAPL.US", 200)])
    provider = RealtimeFallbackProvider(
        primary,
        secondary,
        policy=FallbackPolicy(enabled=True),
    )

    result = provider.get_realtime(symbols=["AAPL.US"])

    assert result.to_dicts() == [{"symbol": "AAPL.US", "last_price": 200.0, "source": "test"}]
    assert secondary.calls == 1


def test_failed_sources_use_recent_cache():
    primary = FakeProvider("primary", [quote("AAPL.US", 200), RuntimeError("network")])
    secondary = FakeProvider("secondary", [pl.DataFrame(), pl.DataFrame()])
    provider = RealtimeFallbackProvider(
        primary,
        secondary,
        policy=FallbackPolicy(enabled=True, cache_ttl_seconds=1),
    )

    first = provider.get_realtime(symbols=["AAPL.US"])
    second = provider.get_realtime(symbols=["AAPL.US"])

    assert first["last_price"].to_list() == [200.0]
    assert second["last_price"].to_list() == [200.0]


def test_expired_cache_returns_empty_without_fabricated_data():
    primary = FakeProvider("primary", [quote("AAPL.US", 200), pl.DataFrame()])
    secondary = FakeProvider("secondary", [pl.DataFrame(), pl.DataFrame()])
    provider = RealtimeFallbackProvider(
        primary,
        secondary,
        policy=FallbackPolicy(enabled=True, cache_ttl_seconds=0),
    )

    provider.get_realtime(symbols=["AAPL.US"])
    time.sleep(0.001)
    result = provider.get_realtime(symbols=["AAPL.US"])

    assert result.is_empty()


def test_policy_rejects_invalid_timeout():
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        FallbackPolicy(request_timeout_seconds=0)
