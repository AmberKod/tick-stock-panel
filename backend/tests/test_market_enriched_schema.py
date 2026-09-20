"""Incremental HK/US enriched writes must match the directory's schema baseline.

scan_parquet requires one schema across the whole per-symbol partition directory,
so an incremental daily write may never introduce a second schema (legacy 64-col
Datetime('us') vs current 76-col Date). See MEMORY.md schema-consistency notes.
"""
from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.tickflow.market_daily import (
    HK_US_ENRICHED_DIR,
    adapt_enriched_for_write,
    downgrade_enriched,
    legacy_enriched_columns,
)

_IDENTITY_COLS = ["price_schema_version", "raw_price_verified", "price_adjustment", "volume_unit", "currency"]


def _current_schema_frame(symbol: str = "AAPL.US") -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol],
        "date": [date(2026, 9, 10), date(2026, 9, 11)],
        "close": [100.0, 101.0],
        "momentum_20d": [0.1, 0.2],
        "observed_at": ["t1", "t1"],
        "price_schema_version": [1, 1],
        "raw_price_verified": [True, True],
        "price_adjustment": ["unadjusted", "unadjusted"],
        "volume_unit": ["share", "share"],
        "currency": ["USD", "USD"],
    })


def _write_legacy_partition(root, symbol: str) -> None:
    frame = (
        _current_schema_frame(symbol)
        .drop("observed_at", *_IDENTITY_COLS)
        .with_columns(pl.col("date").cast(pl.Datetime("us")))
    )
    path = root / HK_US_ENRICHED_DIR / f"symbol={symbol}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)


def test_legacy_directory_downgrades_incremental_write(tmp_path):
    _write_legacy_partition(tmp_path, "MSFT.US")
    adapted = adapt_enriched_for_write(_current_schema_frame("AAPL.US"), tmp_path, "AAPL.US")
    assert "price_schema_version" not in adapted.columns
    assert adapted.schema["date"] == pl.Datetime("us")
    assert adapted.columns == legacy_enriched_columns(tmp_path)


def test_empty_directory_keeps_current_schema(tmp_path):
    adapted = adapt_enriched_for_write(_current_schema_frame("AAPL.US"), tmp_path, "AAPL.US")
    assert "price_schema_version" in adapted.columns
    assert adapted.schema["date"] == pl.Date


def test_migrated_symbol_keeps_its_new_schema(tmp_path):
    _write_legacy_partition(tmp_path, "MSFT.US")
    new_target = tmp_path / HK_US_ENRICHED_DIR / "symbol=AAPL.US" / "part.parquet"
    new_target.parent.mkdir(parents=True, exist_ok=True)
    _current_schema_frame("AAPL.US").write_parquet(new_target)
    adapted = adapt_enriched_for_write(_current_schema_frame("AAPL.US"), tmp_path, "AAPL.US")
    assert "price_schema_version" in adapted.columns
    assert adapted.schema["date"] == pl.Date


def test_downgrade_reports_missing_legacy_columns():
    legacy_cols = ["symbol", "date", "close", "not_computable"]
    with pytest.raises(ValueError, match="旧 enriched schema 列缺失"):
        downgrade_enriched(_current_schema_frame(), legacy_cols)


def test_us_incremental_sync_never_mixes_schemas(tmp_path):
    from app.services.hk_data_adapter import sync_hk_daily_to_enriched

    _write_legacy_partition(tmp_path, "MSFT.US")
    raw = pl.DataFrame({
        "symbol": ["AAPL.US", "AAPL.US"], "date": [date(2026, 1, 2), date(2026, 1, 5)],
        "open": [10.0, 10.5], "high": [11.0, 12.0], "low": [9.0, 10.0],
        "close": [10.0, 11.0], "volume": [1000.0, 1200.0], "amount": [10500.0, 13100.0],
    }).with_columns(
        pl.lit("fixture").alias("source"), pl.lit("unadjusted").alias("price_adjustment"),
        pl.lit("USD").alias("currency"),
    )
    assert sync_hk_daily_to_enriched("AAPL.US", tmp_path, raw=raw, raise_errors=True) == 1
    parts = sorted((tmp_path / HK_US_ENRICHED_DIR).glob("symbol=*/part.parquet"))
    assert len(parts) == 2
    # Whole-directory scan must succeed with one schema (this is the regression
    # that previously broke regime / matrix / overview).
    scanned = pl.scan_parquet(tmp_path / HK_US_ENRICHED_DIR).collect()
    assert scanned.height == 4
    assert scanned.schema["date"] == pl.Datetime("us")
    assert "price_schema_version" not in scanned.columns
