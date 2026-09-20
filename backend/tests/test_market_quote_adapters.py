"""港美股第三方行情字段适配契约测试。"""
from datetime import UTC, datetime

import pytest

from app.data_providers.adapters import normalize_quote_row, normalize_quote_rows


def test_hk_percent_and_units_are_normalized():
    row = normalize_quote_row(
        {
            "code": "00700.HK", "title": "腾讯控股", "price": "300.5",
            "pre_close": "295", "pct": "1.86", "volume": "12345", "amount": "3700",
            "ts": 1_700_000_000,
        },
        market="hk", source="allstock", pct_unit="percent",
        amount_unit="ten_thousand", volume_unit="hands",
    )
    assert row is not None
    assert row["symbol"] == "00700.HK"
    assert row["last_price"] == pytest.approx(300.5)
    assert row["prev_close"] == pytest.approx(295)
    assert row["change_pct"] == pytest.approx(1.86)
    assert row["amount"] == pytest.approx(37_000_000)
    assert row["volume"] == pytest.approx(1_234_500)
    assert row["timestamp"] == 1_700_000_000_000
    assert row["market"] == "hk"


def test_us_fraction_fields_and_aliases_are_normalized():
    row = normalize_quote_row(
        {
            "ticker": "AAPL.US", "name": "Apple", "close": 200,
            "previous_close": 198, "change": "0.010101", "turnover": "1000",
            "timestamp": datetime(2026, 8, 31, 14, 30, tzinfo=UTC),
        },
        market="us", source="yfinance", pct_unit="fraction",
    )
    assert row is not None
    assert row["last_price"] == 200
    assert row["prev_close"] == 198
    # 小数制输入 (0.010101 = 1.0101%) → 统一输出百分制 (与港股/美股前端口径一致)
    assert row["change_pct"] == pytest.approx(1.0101)
    assert row["timestamp"] == int(datetime(2026, 8, 31, 14, 30, tzinfo=UTC).timestamp() * 1000)
    assert row["market"] == "us"


def test_rows_drop_missing_symbol_and_keep_optional_nulls():
    rows = normalize_quote_rows(
        [{"price": 1}, {"symbol": "MSFT.US", "price": "bad", "ts": "2026-08-31T14:30:00Z"}],
        market="us", source="test",
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "MSFT.US"
    assert rows[0]["last_price"] is None
    assert rows[0]["timestamp"] is not None
