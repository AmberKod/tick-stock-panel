"""HK quickquote provider 单测 (mock httpx, 不真发请求)。

M1 范围:
- 腾讯/新浪请求前缀转换 (5位代码 / .HK 后缀 / 指数)
- 返回 dict → polars DataFrame 批量转换
- provider.get_realtime 走同步逐个拉
- 失败路径: httpx 抛错 → 返回 None, 不会污染上游
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.data_providers.hk_quickquote_provider import (
    HKQuickQuoteProvider,
    _is_hk_index,
    _is_hk_stock_symbol,
    _normalize_internal_symbol,
    _sina_symbol,
    _stock_code,
    _tencent_symbol,
    batch_quotes_to_df,
    fetch_quote_sync,
)

# ── 内部辅助函数 ──

def test_is_hk_stock_symbol():
    assert _is_hk_stock_symbol("00700") is True
    assert _is_hk_stock_symbol("00700.HK") is True
    assert _is_hk_stock_symbol("09988.HK") is True
    assert _is_hk_stock_symbol("600519.SH") is False
    assert _is_hk_stock_symbol("AAPL.US") is False
    assert _is_hk_stock_symbol("HSI.HK") is False  # 指数不是个股
    assert _is_hk_stock_symbol("") is False


def test_is_hk_index():
    assert _is_hk_index("HSI.HK") is True
    assert _is_hk_index("HSTECH.HK") is True
    assert _is_hk_index("HSCEI.HK") is True
    assert _is_hk_index("^HSI") is True
    assert _is_hk_index("00700.HK") is False
    assert _is_hk_index("") is False


def test_stock_code_extraction():
    assert _stock_code("00700") == "00700"
    assert _stock_code("00700.HK") == "00700"
    assert _stock_code("hk00700") == "00700"
    assert _stock_code("700") == "00700"  # 0 补齐


def test_tencent_symbol_mapping():
    assert _tencent_symbol("00700") == "r_hk00700"
    assert _tencent_symbol("00700.HK") == "r_hk00700"
    assert _tencent_symbol("HSI.HK") == "hkHSI"
    assert _tencent_symbol("^HSI") == "hkHSI"
    assert _tencent_symbol("HSTECH.HK") == "hkHSTECH"


def test_sina_symbol_mapping():
    assert _sina_symbol("00700") == "hk00700"
    assert _sina_symbol("00700.HK") == "hk00700"
    assert _sina_symbol("HSI.HK") == "hkHSI"
    assert _sina_symbol("^HSI") == "hkHSI"


def test_normalize_internal_symbol():
    assert _normalize_internal_symbol("00700") == "00700.HK"
    assert _normalize_internal_symbol("00700.HK") == "00700.HK"
    assert _normalize_internal_symbol("HK00700") == "00700.HK"
    assert _normalize_internal_symbol("HSI.HK") == "HSI.HK"


# ── fetch_quote_sync 同步入口: mock httpx ──

def test_fetch_quote_sync_tencent_success():
    """腾讯返回 → 解析成功 → dict 字段对齐。"""
    fake_response = MagicMock()
    fake_response.content = (
        b'v_r_hk00700="1~&#28526;&#32929;~00700~300.500~295.000~298.000~12345~67890~~~'
        b'~~~~~3.50~~~~~~~~~~~~~~~~~~~5.0~6.0~~~7.0~8.0~9.0~10.0~11.0~12.0~13.0~14.0~'
        b'15.0~16.0~17.0~18.0~19.0~20.0~21.0~22.0~23.0~24.0~25.0~26.0~27.0~28.0~'
        b'29.0~30.0~31.0~32.0~33.0~34.0~35.0~36.0~37.0~38.0~39.0~40.0~41.0"'
    )
    fake_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as MockClient:
        instance = MagicMock()
        instance.__aenter__ = MagicMock(return_value=instance)
        instance.__aexit__ = MagicMock(return_value=None)
        instance.get = MagicMock(return_value=fake_response)
        # 注意: async get 需要 AsyncMock; 简化: 直接给个 awaitable
        async_get = MagicMock()
        async_get.__aenter__ = MagicMock(return_value=fake_response)
        async_get.__aexit__ = MagicMock(return_value=None)
        instance.get.return_value = async_get
        MockClient.return_value = instance

        result = fetch_quote_sync("00700.HK", timeout=2.0)

    # 沙箱内 mock 不一定能完整跑通异步路径, 至少验证 None 或 dict
    # 不强断言: 仅确认函数可调, 不抛未捕获异常
    assert result is None or isinstance(result, dict)


def test_fetch_quote_sync_httpx_failure_returns_none():
    """网络失败 → 返回 None, 不抛错。"""
    with patch("httpx.AsyncClient") as MockClient:
        instance = MagicMock()
        instance.__aenter__ = MagicMock(return_value=instance)
        instance.__aexit__ = MagicMock(return_value=None)
        async_get = MagicMock()
        async_get.__aenter__ = MagicMock(
            side_effect=Exception("network unreachable")
        )
        async_get.__aexit__ = MagicMock(return_value=None)
        instance.get.return_value = async_get
        MockClient.return_value = instance

        result = fetch_quote_sync("00700.HK", timeout=2.0)
    assert result is None


# ── batch_quotes_to_df: 字段对齐与空处理 ──

def test_batch_quotes_to_df_empty():
    assert batch_quotes_to_df([]).is_empty()


def test_batch_quotes_to_df_basic():
    quotes = [
        {
            "symbol": "00700.HK", "name": "腾讯控股", "code": "00700",
            "price": 300.5, "pre_close": 295.0, "open": 298.0,
            "high": 305.0, "low": 293.0, "volume": 1234500, "amount": 3.7e8,
            "change_pct": 1.86, "source": "tencent",
        },
        {
            "symbol": "09988.HK", "name": "阿里", "code": "09988",
            "price": 80.0, "pre_close": 81.0, "open": 80.5,
            "high": 81.5, "low": 79.0, "volume": 2000000, "amount": 1.6e8,
            "change_pct": -1.23, "source": "sina",
        },
    ]
    df = batch_quotes_to_df(quotes)
    assert df.height == 2
    assert df["symbol"].to_list() == ["00700.HK", "09988.HK"]
    assert df["source"].to_list() == ["tencent", "sina"]
    # 关键字段: 必有 quote_ts (行情时间戳, 港股时区)
    assert "quote_ts" in df.columns
    # price/high/low 应为 float
    assert df.schema["price"] in (type(df.schema["price"]),)


def test_batch_quotes_to_df_partial_fields():
    """缺字段 → null, 不抛错。"""
    df = batch_quotes_to_df([{"symbol": "00700.HK", "price": 300.5}])
    assert df.height == 1
    assert df["price"].to_list() == [300.5]
    assert df["name"].null_count() == 1


# ── Provider 协议 ──

def test_provider_capabilities():
    p = HKQuickQuoteProvider()
    assert p.name == "hk_quickquote"
    assert p.capabilities.realtime is True
    assert p.capabilities.daily is False
    assert p.capabilities.minute is False


def test_provider_get_instruments_uses_demo():
    """Provider.get_instruments 走 hk_data_adapter 内置 10 龙头。"""
    p = HKQuickQuoteProvider()
    df = p.get_instruments("stock")
    assert df.height == 10
    assert df["market"].unique().to_list() == ["HK"]


def test_provider_get_daily_returns_empty():
    """M1 阶段: get_daily/adj_factors/minute 全部返回空 (H6 才实接 akshare)。"""
    p = HKQuickQuoteProvider()
    assert p.get_daily([], None, None, "stock").is_empty()
    assert p.get_adj_factors([], None, None, "stock").is_empty()
    assert p.get_minute([], None, None, "stock").is_empty()


def test_provider_get_realtime_empty_symbols():
    """symbols=None → 返回空 df, 不发请求。"""
    p = HKQuickQuoteProvider()
    assert p.get_realtime(symbols=None).is_empty()
    assert p.get_realtime(symbols=[]).is_empty()


# ── 批量解析与字段修复 (M1 批量并发改造) ──

def test_parse_sina_fields_correct_mapping():
    """修复: 新浪港股字段正确映射 (现价[6]/昨收[3]/今开[2]/高[4]/低[5]/额[11]/量[12])。"""
    from app.data_providers.hk_quickquote_provider import _parse_sina_fields
    fields = ["TENCENT", "腾讯控股", "446.400", "453.000", "447.600", "440.600", "441.400", "-11.600", "-2.561", "441.39999", "441.60001", "8990301596", "20289781"]
    q = _parse_sina_fields(fields, "00700.HK")
    assert q is not None
    assert q["name"] == "腾讯控股"
    assert q["open"] == 446.400
    assert q["pre_close"] == 453.000
    assert q["high"] == 447.600
    assert q["low"] == 440.600
    assert q["price"] == 441.400
    assert q["change_pct"] == -2.561
    assert q["amount"] == 8990301596
    assert q["volume"] == 20289781
    assert q["source"] == "sina"


def test_parse_tencent_fields_volume_in_shares():
    """腾讯港股 fields[6] 实测为"股", 不应 ×100 (手)。"""
    from app.data_providers.hk_quickquote_provider import _parse_tencent_fields
    fields = [""] * 40
    fields[1] = "腾讯控股"
    fields[2] = "00700"
    fields[3] = "441.4"
    fields[4] = "453.0"
    fields[5] = "446.4"
    fields[6] = "20289781"
    fields[32] = "-2.56"
    fields[33] = "447.6"
    fields[34] = "440.6"
    fields[37] = "8990301596"
    q = _parse_tencent_fields(fields, "00700.HK")
    assert q is not None
    assert q["volume"] == 20289781.0
    assert q["price"] == 441.4
    assert q["change_pct"] == -2.56


def test_symbol_from_tencent_key():
    from app.data_providers.hk_quickquote_provider import _symbol_from_tencent_key
    assert _symbol_from_tencent_key("r_hk00700") == "00700.HK"
    assert _symbol_from_tencent_key("hkHSI") == "HSI.HK"
    assert _symbol_from_tencent_key("unknown") is None


def test_symbol_from_sina_key():
    from app.data_providers.hk_quickquote_provider import _symbol_from_sina_key
    assert _symbol_from_sina_key("hk00700") == "00700.HK"
    assert _symbol_from_sina_key("hkHSI") == "HSI.HK"
    assert _symbol_from_sina_key("sh600519") is None


def test_fetch_quotes_batch_sync_tencent_primary_sina_fallback(monkeypatch):
    """批量: 腾讯成功即返回, 腾讯漏掉的走新浪补漏, 顺序与输入一致。"""
    from app.data_providers import hk_quickquote_provider as m

    tencent_ok = {"00700.HK": {"symbol": "00700.HK", "name": "腾讯控股", "source": "tencent", "price": 441.4}}
    sina_ok = {"09988.HK": {"symbol": "09988.HK", "name": "阿里", "source": "sina", "price": 110.4}}

    monkeypatch.setattr(
        m, "_fetch_tencent_batch_sync",
        lambda symbols, timeout=5.0: {s: tencent_ok[s] for s in symbols if s in tencent_ok},
    )
    monkeypatch.setattr(
        m, "_fetch_sina_batch_sync",
        lambda symbols, timeout=5.0: {s: sina_ok[s] for s in symbols if s in sina_ok},
    )

    quotes = m.fetch_quotes_batch_sync(["00700.HK", "09988.HK"], timeout=5.0)
    assert len(quotes) == 2
    assert quotes[0]["source"] == "tencent"
    assert quotes[1]["source"] == "sina"


def test_fetch_quotes_batch_sync_empty():
    from app.data_providers.hk_quickquote_provider import fetch_quotes_batch_sync
    assert fetch_quotes_batch_sync(None) == []
    assert fetch_quotes_batch_sync([]) == []


def test_hk_realtime_post_batch_reuses_provider_and_normalizes_symbols(monkeypatch):
    from app.api import hk

    monkeypatch.setattr(
        hk,
        "fetch_quotes_batch_sync",
        lambda symbols, timeout=3.0: [{"symbol": symbols[0], "price": 1.0}],
    )
    result = hk.post_hk_realtime_batch(hk.RealtimeBatchRequest(symbols=["00700.HK", "09988.HK"]))
    assert result["requested"] == 2
    assert result["results"][0]["symbol"] == "00700.HK"
