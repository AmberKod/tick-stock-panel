"""yfinance 限流熔断退避测试。

背景: Yahoo 免费档为 IP 级滑动窗口限流。09-15 实测美股全量补跑触发限流后,
provider 无退避全速空烧 (105 次/分钟连打 429), 拖长被拦时长且 0 数据收益。
熔断行为与腾讯 WAF (test_tencent_market_provider) 同构:
- 连续 5 次限流 → 打开熔断, 冷却期内 0 网络请求 (本地快速失败)
- 冷却结束后半开: 首个请求失败 → 冷却翻倍 (10→20→40 分钟, 封顶 60)
- 任一成功 → 完全复位
"""
from __future__ import annotations

import time

import polars as pl
import pytest

from app.data_providers import yfinance_provider as mod


class _RateLimitError(Exception):
    pass


class _FakeTicker:
    def __init__(self, exc: Exception | None):
        self._exc = exc

    def history(self, **kwargs):
        if self._exc is not None:
            raise self._exc
        import pandas as pd

        return pd.DataFrame(
            {
                "Open": [1.0], "High": [1.0], "Low": [1.0],
                "Close": [1.0], "Volume": [100],
            },
            index=pd.DatetimeIndex(["2026-09-15"], name="Date"),
        )


@pytest.fixture(autouse=True)
def _reset_circuit():
    mod.yf_circuit_reset()
    yield
    mod.yf_circuit_reset()


def _patch_yf(monkeypatch, exc: Exception | None):
    class _FakeYf:
        Ticker = staticmethod(lambda sym: _FakeTicker(exc))

    monkeypatch.setattr(mod, "_try_import_yf", lambda: _FakeYf)


def test_circuit_opens_after_consecutive_rate_limits(monkeypatch):
    _patch_yf(monkeypatch, _RateLimitError("Too Many Requests. Rate limited. Try after a while"))
    provider = mod.YFinanceProvider()

    df = provider.get_daily(["A.US", "B.US", "C.US", "D.US", "E.US", "F.US"], None, None, "stock")

    # 前 5 只触发打开, 第 6 只本地快速失败 —— 全程只发 5 次网络请求
    assert df.height == 0
    assert mod._YF_CIRCUIT["opens"] == 1
    assert mod._YF_CIRCUIT["blocked_until"] > time.monotonic()


def test_blocked_circuit_makes_zero_network_calls(monkeypatch):
    _patch_yf(monkeypatch, _RateLimitError("Too Many Requests"))
    provider = mod.YFinanceProvider()
    provider.get_daily(["A.US"] * 5, None, None, "stock")
    assert mod._YF_CIRCUIT["opens"] == 1

    # 熔断打开后换成能成功的 Ticker, 但循环应直接 break, 一次都不调
    calls = {"n": 0}

    class _CountingTicker:
        def history(self, **kwargs):
            calls["n"] += 1
            raise AssertionError("熔断期内不应发起网络请求")

    class _FakeYf2:
        Ticker = staticmethod(lambda sym: _CountingTicker())

    monkeypatch.setattr(mod, "_try_import_yf", lambda: _FakeYf2)
    provider.get_daily(["A.US", "B.US"], None, None, "stock")
    assert calls["n"] == 0


def test_half_open_failure_doubles_cooldown(monkeypatch):
    # 先熔断一次 (opens=1, 冷却 10 分钟)
    _patch_yf(monkeypatch, _RateLimitError("Too Many Requests"))
    provider = mod.YFinanceProvider()
    provider.get_daily(["A.US"] * 5, None, None, "stock")
    assert mod._YF_CIRCUIT["opens"] == 1

    # 推进时钟越过冷却期 → 半开放行首个请求; 探测仍失败 → 翻倍冷却
    real_monotonic = time.monotonic
    expired = mod._YF_CIRCUIT["blocked_until"] + 1.0
    monkeypatch.setattr(
        mod._time, "monotonic", lambda: max(real_monotonic(), expired),
    )
    provider.get_daily(["A.US"], None, None, "stock")

    assert mod._YF_CIRCUIT["opens"] == 2
    remaining = mod._YF_CIRCUIT["blocked_until"] - expired
    # 冷却 = base * 2^1 = 20 分钟 (允许 1 秒执行误差)
    assert mod._YF_COOLDOWN_BASE_SECONDS * 2 - 5 <= remaining <= mod._YF_COOLDOWN_BASE_SECONDS * 2


def test_success_resets_circuit(monkeypatch):
    mod._YF_CIRCUIT.update(
        failures=3, opens=0, blocked_until=0.0,
    )
    _patch_yf(monkeypatch, None)  # 成功
    provider = mod.YFinanceProvider()

    df = provider.get_daily(["AAPL.US"], None, None, "stock")

    assert df.height == 1
    assert mod._YF_CIRCUIT == {"failures": 0, "opens": 0, "blocked_until": 0.0}


def test_non_rate_limit_error_does_not_trip_circuit(monkeypatch):
    _patch_yf(monkeypatch, RuntimeError("connection reset"))
    provider = mod.YFinanceProvider()

    provider.get_daily(["A.US", "B.US"], None, None, "stock")

    assert mod._YF_CIRCUIT["failures"] == 0
    assert mod._YF_CIRCUIT["opens"] == 0


def test_adj_factors_respects_circuit(monkeypatch):
    _patch_yf(monkeypatch, _RateLimitError("Too Many Requests"))
    provider = mod.YFinanceProvider()
    provider.get_adj_factors(["A.US"] * 5, None, None, "stock")

    assert mod._YF_CIRCUIT["opens"] == 1

    mod._YF_CIRCUIT.update(blocked_until=time.monotonic() + 60)
    df = provider.get_adj_factors(["C.US"], None, None, "stock")
    assert isinstance(df, pl.DataFrame)


def test_rate_limit_marker_matching():
    assert mod._yf_is_rate_limited(_RateLimitError("Too Many Requests. Rate limited"))
    assert mod._yf_is_rate_limited(_RateLimitError("HTTP Error 429"))
    assert not mod._yf_is_rate_limited(RuntimeError("no data, symbol may be delisted"))
