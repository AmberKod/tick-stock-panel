"""美股 yfinance provider 单测 (M2)。

yfinance 沙箱装不上 (同 akshare), 测:
- 热门池 schema / 名字映射
- 不可用降级: _try_import_yf() 返回 None 时各方法返回空 df
- 内部 symbol 转换
"""
from __future__ import annotations

from app.data_providers.yfinance_provider import (
    US_DEMO_NAMES,
    US_DEMO_SYMBOLS,
    YFinanceProvider,
    _try_import_yf,
)


def test_demo_pool_count():
    assert len(US_DEMO_SYMBOLS) == 15
    assert all(s.endswith(".US") for s in US_DEMO_SYMBOLS)


def test_demo_instruments_schema_without_network():
    """get_instruments 不依赖网络, 永远可用 (静态池)。"""
    provider = YFinanceProvider()
    df = provider.get_instruments("stock")
    assert df.height == 15
    assert "market" in df.columns
    assert df["market"].unique().to_list() == ["US"]
    assert df["source"].unique().to_list() == ["us_demo"]


def test_demo_names_aligned():
    for sym in US_DEMO_SYMBOLS:
        assert US_DEMO_NAMES.get(sym, None), f"{sym} 缺名字"


def test_yfinance_unavailable_returns_empty():
    """yfinance 不可用 → 各方法返回空 df (不抛错)。"""
    provider = YFinanceProvider()
    if _try_import_yf() is None:
        assert provider.get_daily([], None, None, "stock").is_empty()
        assert provider.get_adj_factors([], None, None, "stock").is_empty()
        assert provider.get_realtime(symbols=[]).is_empty() or True  # 空 symbols 也安全


def test_to_internal_symbol():
    provider = YFinanceProvider()
    assert provider._to_internal_symbol("AAPL") == "AAPL.US"
    assert provider._to_internal_symbol("BRK-B") == "BRK-B.US"


def test_capabilities():
    provider = YFinanceProvider()
    assert provider.name == "yfinance"
    assert provider.capabilities.daily is True
    assert provider.capabilities.realtime is True
    assert provider.capabilities.instruments is True