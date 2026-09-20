"""可配置的多数据源实时行情回退包装器。

该模块只负责编排 provider，不注册默认数据源。默认关闭回退，调用方必须显式
传入 ``enabled=True``，避免未经验证的外部源改变现有行情链路。
"""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any

import polars as pl

from app.data_providers.base import MarketDataProvider


@dataclass(frozen=True)
class FallbackPolicy:
    enabled: bool = False
    cache_ttl_seconds: float = 30.0
    request_timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be non-negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")


class RealtimeFallbackProvider:
    """先调主源，失败/空结果时可选调备用源，并短暂缓存成功结果。

    provider 的同步调用无法被安全地强制中断，因此 timeout 在调用返回后用于
    判定结果是否过期；真正的网络超时应由各 provider 自己配置。这样不会在线程
    泄漏的情况下伪装出可取消的请求。
    """

    def __init__(
        self,
        primary: MarketDataProvider,
        secondary: MarketDataProvider,
        *,
        policy: FallbackPolicy | None = None,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.policy = policy or FallbackPolicy()
        self.name = f"fallback_{primary.name}_{secondary.name}"
        self.capabilities = primary.capabilities
        self._cache: dict[tuple[str, ...], tuple[float, pl.DataFrame]] = {}

    def _cached(self, key: tuple[str, ...], now: float) -> pl.DataFrame | None:
        item = self._cache.get(key)
        if item and self.policy.cache_ttl_seconds > 0 and now - item[0] <= self.policy.cache_ttl_seconds:
            return item[1].clone()
        return None

    def _remember(self, key: tuple[str, ...], frame: pl.DataFrame, now: float) -> pl.DataFrame:
        cached = frame.clone()
        self._cache[key] = (now, cached)
        return cached.clone()

    @staticmethod
    def _usable(frame: Any) -> bool:
        return isinstance(frame, pl.DataFrame) and not frame.is_empty()

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> pl.DataFrame:
        del universes
        requested = tuple(dict.fromkeys(str(s).strip().upper() for s in (symbols or []) if str(s).strip()))
        if not requested:
            return pl.DataFrame()
        key = requested
        monotonic()

        try:
            started = monotonic()
            primary_frame = self.primary.get_realtime(symbols=list(requested))
            if self._usable(primary_frame) and monotonic() - started <= self.policy.request_timeout_seconds:
                return self._remember(key, primary_frame, monotonic())
        except Exception:
            primary_frame = pl.DataFrame()

        if not self.policy.enabled:
            cached = self._cached(key, monotonic())
            return cached if cached is not None else pl.DataFrame()

        try:
            started = monotonic()
            secondary_frame = self.secondary.get_realtime(symbols=list(requested))
            if self._usable(secondary_frame) and monotonic() - started <= self.policy.request_timeout_seconds:
                return self._remember(key, secondary_frame, monotonic())
        except Exception:
            pass

        cached = self._cached(key, monotonic())
        return cached if cached is not None else pl.DataFrame()

    def close(self) -> None:
        for provider in (self.primary, self.secondary):
            close = getattr(provider, "close", None)
            if callable(close):
                close()
