"""腾讯多市场候选 Provider 的离线契约测试。"""

import json

import polars as pl
import pytest

from app.data_providers.normalizer import DAILY_COLS
from app.data_providers.tencent_market_provider import (
    _key_to_symbol,
    _market_code,
    parse_tencent_kline,
    parse_tencent_quote,
)


def test_market_code_mapping():
    assert _market_code("00700.HK", "hk") == "hk00700"
    assert _market_code("AAPL.US", "us") == "usAAPL"


def test_parse_quote_normalizes_tencent_percent():
    fields = [""] * 35
    fields[1] = "Apple"
    fields[3] = "200"
    fields[4] = "198"
    fields[5] = "199"
    fields[6] = "1000"
    fields[30] = "2026-08-31 11:47:19"
    fields[32] = "1.01"
    fields[33] = "201"
    fields[34] = "197"
    payload = 'v_usAAPL="' + "~".join(fields) + '"'
    row = parse_tencent_quote(payload, symbol="AAPL.US", market="us")
    assert row is not None
    assert row["last_price"] == 200
    assert row["prev_close"] == 198
    assert row["change_pct"] == 1.01
    assert row["timestamp"] is not None
    assert row["market"] == "us"


def test_parse_quote_normalizes_market_units_and_source_time():
    fields = [""] * 40
    fields[1] = "腾讯控股"
    fields[3] = "453"
    fields[4] = "455.2"
    fields[5] = "451"
    fields[6] = "20149770"
    fields[30] = "2026/08/31 16:08:50"
    fields[32] = "-0.48"
    fields[33] = "456.2"
    fields[34] = "446"
    fields[37] = "909317.7"
    payload = 'v_r_hk00700="' + "~".join(fields) + '"'
    row = parse_tencent_quote(payload, symbol="00700.HK", market="hk")
    assert row is not None
    assert row["volume"] == 20149770
    assert row["amount"] == 909317.7
    assert row["change_pct"] == -0.48
    assert row["timestamp"] is not None


def test_parse_kline_documented_shape():
    payload = '{"data":{"hk00700":{"qfqday":[["2026-08-28","300","301","305","298","12345"]]}}}'
    frame = parse_tencent_kline(payload, symbol="00700.HK")
    assert frame.height == 1
    assert frame["date"].to_list()[0].isoformat() == "2026-08-28"
    assert frame["open"].to_list() == [300.0]
    assert frame["close"].to_list() == [301.0]
    assert frame["volume"].to_list() == [12345.0]


@pytest.mark.parametrize(
    ("symbol", "key", "series", "adjustment"),
    [
        ("00700.HK", "hk00700", "qfqday", "forward_adjusted"),
        ("80700.HK", "hk80700", "day", "unadjusted"),
        ("AAPL.US", "usAAPL", "qfqday", "forward_adjusted"),
    ],
)
def test_kline_metadata_preserves_only_supported_provenance(symbol, key, series, adjustment):
    payload = json.dumps({"data": {key: {series: [["2026-08-28", "300", "301", "305", "298", "12345"]]}}})

    frame = parse_tencent_kline(payload, symbol=symbol, source="candidate_fixture")

    assert frame.columns == DAILY_COLS
    row = frame.row(0, named=True)
    assert row["source"] == "candidate_fixture"
    assert row["symbol"] == symbol
    assert [row[name] for name in ("open", "close", "high", "low", "volume")] == [300.0, 301.0, 305.0, 298.0, 12345.0]
    assert row["price_adjustment"] == adjustment
    assert row["raw_price_verified"] is False
    assert frame.schema["raw_price_verified"] == pl.Boolean
    assert frame.schema["price_schema_version"] == pl.Int64
    for name in (
        "currency", "volume_unit", "amount_source", "observed_at",
        "adjustment_source", "adjustment_version", "adjustment_as_of", "verification_source",
    ):
        assert row[name] is None
        assert frame.schema[name] == pl.String
    assert row["price_schema_version"] is None
    assert row["quote_ts"] is None


def test_kline_price_type_comes_from_selected_series():
    payload = json.dumps({"data": {"hk00700": {
        "qfqday": [],
        "day": [["2026-08-28", "400", "401", "405", "398", "12345"]],
    }}})

    frame = parse_tencent_kline(payload, symbol="00700.HK")

    assert frame["open"].to_list() == [400.0]
    assert frame["price_adjustment"].to_list() == ["unadjusted"]
    assert frame["raw_price_verified"].to_list() == [False]


def test_key_to_symbol_mapping():
    assert _key_to_symbol("usAAPL", "us") == "AAPL.US"
    assert _key_to_symbol("hk00700", "hk") == "00700.HK"
    assert _key_to_symbol("r_hk00700", "hk") == "00700.HK"
    assert _key_to_symbol("", "us") is None


def test_get_realtime_batch_parses_multiple_lines():
    """批量: 一次请求返回多行, 逐行解析成多只行情。"""
    from app.data_providers.tencent_market_provider import TencentMultiMarketProvider

    def make_line(key, code, price, pre_close, pct):
        f = [""] * 35
        f[1] = code
        f[2] = code
        f[3] = str(price)
        f[4] = str(pre_close)
        f[5] = str(price)
        f[6] = "1000"
        f[30] = "2026-09-01 10:02:00"
        f[32] = str(pct)
        f[33] = str(price + 1)
        f[34] = str(price - 1)
        return f'v_{key}="' + "~".join(f) + '"'

    payload = make_line("usAAPL", "苹果", 322.87, 316.85, 1.90) + ";\n" + make_line("usMSFT", "微软", 504.0, 507.29, -0.65)

    class _FakeResp:
        content = payload.encode("gbk")

        def raise_for_status(self):
            return None

    class _FakeClient:
        def get(self, url, headers=None):
            return _FakeResp()

        def close(self):
            return None

    p = TencentMultiMarketProvider("us", timeout=5.0)
    p._client = _FakeClient()
    df = p.get_realtime(["AAPL.US", "MSFT.US"])
    assert df.height == 2
    syms = sorted(df["symbol"].to_list())
    assert syms == ["AAPL.US", "MSFT.US"]
    assert df.filter(pl.col("symbol") == "AAPL.US")["last_price"].to_list() == [322.87]
