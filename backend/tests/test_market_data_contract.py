from __future__ import annotations

import io
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.data_providers.normalizer import (
    normalize_instruments,
    normalize_lot_size,
    normalize_market_symbols,
)
from app.services import hk_data_adapter, market_daily_sync, market_data_status
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.market_daily import read_market_daily_symbol, write_market_daily_symbol
from app.tickflow.repository import KlineRepository


def _bars(symbol: str, close: float = 11) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol, symbol], "date": [date(2026, 1, 2), date(2026, 1, 5)],
        "open": [10.0, 10.5], "high": [11.0, close + 1], "low": [9.0, 10.0],
        "close": [10.0, close], "volume": [1000.0, 1200.0], "amount": [10500.0, 13100.0],
    })


def _instruments(root, market: str, symbols: list[str]) -> None:
    path = root / "instruments" / f"{market}_instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    normalize_instruments([{"symbol": symbol} for symbol in symbols], "stock", "fixture").write_parquet(path)


@pytest.mark.parametrize("value,expected", [(100, 100), (500.0, 500), ("1,000", 1000), (None, None),
                                             (0, None), (-1, None), (1.2, None), (True, None), ("nan", None)])
def test_lot_size_requires_explicit_positive_integer(value, expected):
    assert normalize_lot_size(value) == expected


def test_normalizer_retains_lots_and_class_share_code():
    frame = normalize_instruments([
        {"symbol": "00700.HK", "lot_size": "100", "lot_size_source": "hkex", "lot_size_as_of": "2026-09-09"},
        {"symbol": "BRK.A.US"},
    ], "stock")
    assert frame.filter(pl.col("symbol") == "00700.HK")["lot_size"].item() == 100
    assert frame.filter(pl.col("symbol") == "BRK.A.US")["code"].item() == "BRK.A"
    assert frame.schema["lot_size"] == pl.Int64


@pytest.mark.parametrize("market,symbol", [("HK", "AAPL.US"), ("US", "00700.HK"), ("HK", "../00700"),
                                          ("US", "../../AAPL"), ("US", "AAPL.US/part"), ("HK", "")])
def test_single_market_symbols_reject_cross_market_and_paths(market, symbol):
    with pytest.raises(ValueError):
        normalize_market_symbols([symbol], market)


def test_class_share_and_short_hk_symbols_are_preserved():
    assert normalize_market_symbols(["brk.a.us", "BRK.A", "BRK-B"], "US") == ["BRK.A.US", "BRK-B.US"]
    assert normalize_market_symbols(["700", "00700.HK"], "HK") == ["00700.HK"]


def test_date_partition_is_read_by_market_indicator_sync(tmp_path):
    path = tmp_path / "kline_daily" / "date=2026-01-05" / "part.parquet"
    path.parent.mkdir(parents=True)
    hk_bars = _bars("00700.HK").with_columns(
        pl.lit("sina_hk_daily").alias("source"), pl.lit("unadjusted").alias("price_adjustment"),
        pl.lit(1).alias("price_schema_version"), pl.lit(True).alias("raw_price_verified"),
        pl.lit("share").alias("volume_unit"), pl.lit("HKD").alias("currency"),
    )
    pl.concat([hk_bars, _bars("AAPL.US")], how="diagonal_relaxed").write_parquet(path)
    factors_path = tmp_path / "adj_factor_hk" / "symbol=00700.HK" / "part.parquet"
    factors_path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["00700.HK"], "trade_date": [date(1900, 1, 1)], "ex_factor": [1.0],
                  "source": ["verified_fixture"], "version": ["v1"], "coverage_end": [date(2026, 1, 5)]}).write_parquet(factors_path)
    result = market_data_status.recompute_market_enriched(tmp_path, "HK")
    assert result["status"] == "completed"
    assert result["symbols"] == ["00700.HK"]
    assert not (tmp_path / "kline_hk_us_enriched" / "symbol=AAPL.US").exists()
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / "symbol=00700.HK" / "part.parquet")
    assert enriched["date"].to_list() == [date(2026, 1, 2), date(2026, 1, 5)]
    assert enriched["close"].to_list() == [10.0, 11.0]
    assert enriched["change_pct"][-1] == pytest.approx(0.1)
    assert enriched["volume"][-1] == 1200
    assert enriched["amount"][-1] == 13100


def test_merge_preserves_old_history_and_does_not_overwrite_corrupt_file(tmp_path):
    first = _bars("BRK.A.US")
    path = write_market_daily_symbol(tmp_path, "BRK.A.US", first)
    next_bar = first.tail(1).with_columns(pl.lit(20.0).alias("close"), pl.lit("provider").alias("source"))
    write_market_daily_symbol(tmp_path, "BRK.A.US", next_bar)
    merged = read_market_daily_symbol(tmp_path, "BRK.A.US")
    assert merged["close"].to_list() == [10.0, 20.0]
    path.write_bytes(b"damaged prior snapshot")
    with pytest.raises(pl.exceptions.ComputeError):
        write_market_daily_symbol(tmp_path, "BRK.A.US", first)
    assert path.read_bytes() == b"damaged prior snapshot"


@pytest.mark.parametrize("column", ["symbol", "date"])
def test_null_identity_in_new_or_old_partition_is_rejected(tmp_path, column):
    valid = _bars("00700.HK")
    invalid = valid.with_columns(pl.lit(None).cast(valid.schema[column]).alias(column))
    with pytest.raises(ValueError):
        write_market_daily_symbol(tmp_path, "00700.HK", invalid)
    path = tmp_path / "kline_daily" / "symbol=00700.HK" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    invalid.write_parquet(path)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_market_daily_symbol(tmp_path, "00700.HK", valid)
    assert path.read_bytes() == before


def test_yfinance_daily_preserves_class_share_units_and_exchange_dates(monkeypatch):
    from app.data_providers import yfinance_provider
    calls = []
    history = pd.DataFrame({"Open": [10, 11], "High": [12, 13], "Low": [9, 10], "Close": [11, 12], "Volume": [100, 150]},
                           index=pd.DatetimeIndex(["2026-03-06", "2026-03-09"], tz="America/New_York", name="Date"))
    def ticker(symbol):
        calls.append(symbol)
        return SimpleNamespace(history=lambda **kwargs: history)
    monkeypatch.setattr(yfinance_provider, "_try_import_yf", lambda: SimpleNamespace(Ticker=ticker))
    result = yfinance_provider.YFinanceProvider().get_daily(
        ["BRK.A.US"], datetime(2026, 3, 1), datetime(2026, 3, 9), "stock",
    )
    assert calls == ["BRK-A"]
    assert result["symbol"].unique().to_list() == ["BRK.A.US"]
    assert result["date"].to_list() == [date(2026, 3, 6), date(2026, 3, 9)]
    assert result["volume"].to_list() == [100, 150]
    assert result["close"].to_list() == [11, 12]
    assert result["amount"].null_count() == 2


def test_recompute_empty_and_partial_are_not_success(tmp_path):
    assert market_data_status.recompute_market_enriched(tmp_path, "US")["status"] == "empty"
    write_market_daily_symbol(tmp_path, "AAPL.US", _bars("AAPL.US"))
    result = market_data_status.recompute_market_enriched(tmp_path, "US", ["AAPL.US", "MISSING.US"])
    assert (result["status"], result["requested"], result["succeeded"], result["skipped"]) == ("completed_with_errors", 2, 1, 1)
    assert result["items"][1]["reason"]


def test_status_is_read_only_and_counts_current_pool_separately(tmp_path, monkeypatch):
    _without_market_daily(monkeypatch)
    monkeypatch.setattr(market_daily_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    _instruments(tmp_path, "us", ["AAPL.US", "MISSING.US"])
    write_market_daily_symbol(tmp_path, "AAPL.US", _bars("AAPL.US"))
    write_market_daily_symbol(tmp_path, "EXTRA.US", _bars("EXTRA.US"))
    before = set(tmp_path.rglob("*"))
    result = market_data_status.get_market_data_status(tmp_path, "US", CapabilitySet())
    assert result["instruments"]["symbols"] == 2
    assert result["daily"]["symbols"] == 2
    assert result["daily"]["target_symbols"] == 1
    assert result["daily"]["extra_symbols"] == 1
    assert result["daily"]["missing_symbols"] == 1
    assert result["daily"]["rows"] == 4
    assert result["daily"]["last_date"] == "2026-01-05"
    assert not result["capabilities"]["daily_download"]
    assert set(tmp_path.rglob("*")) == before


def test_repository_default_pool_is_current_but_explicit_history_can_be_read(tmp_path):
    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    for symbol in ("AAPL.US", "EXTRA.US"):
        write_market_daily_symbol(tmp_path, symbol, _bars(symbol))
        assert hk_data_adapter.sync_hk_daily_to_enriched(symbol, tmp_path) == 1
    assert repo.get_market_enriched_symbols("us") == []
    assert repo.read_market_enriched("us").is_empty()
    _instruments(tmp_path, "us", ["AAPL.US"])
    assert repo.get_market_enriched_symbols("us") == ["AAPL.US"]
    assert repo.read_market_enriched("us")["symbol"].unique().to_list() == ["AAPL.US"]
    assert repo.read_market_enriched("us", symbols=["EXTRA.US"])["symbol"].unique().to_list() == ["EXTRA.US"]
    with pytest.raises(ValueError):
        repo.read_market_enriched("us", symbols=["00700.HK"])


def test_hkex_parser_uses_explicit_currency_lot_and_snapshot_date():
    from app.data_providers.hkex_instruments import parse_hkex_lot_sizes
    buffer = io.BytesIO()
    pd.DataFrame([
        ["List of Securities", None, None], ["Updated as at 09/09/2026", None, None],
        ["Stock Code", "Board Lot", "Trading Currency"],
        ["00001", "500", "HKD"], ["00003", "1,000", "HKD"], ["80001", "100", "RMB"],
        ["00004", "0", "HKD"],
    ]).to_excel(buffer, header=False, index=False)
    result = parse_hkex_lot_sizes(buffer.getvalue())
    assert result["symbol"].to_list() == ["00001.HK", "00003.HK", "00004.HK", "80001.HK"]
    assert result["lot_size"].to_list() == [500, 1000, None, 100]
    assert result["currency"].to_list() == ["HKD", "HKD", "HKD", "CNY"]
    assert result["lot_size_as_of"].unique().to_list() == [date(2026, 9, 9)]


def test_lot_sync_only_enriches_existing_universe(tmp_path, monkeypatch):
    from app.data_providers import hkex_instruments
    _instruments(tmp_path, "hk", ["00001.HK", "00002.HK"])
    path = tmp_path / "instruments" / "hk_instruments.parquet"
    pl.read_parquet(path).with_columns(pl.lit("keep me").alias("custom_column")).write_parquet(path)
    official = pl.DataFrame({"symbol": ["00001.HK", "09999.HK"], "lot_size": [500, 100],
                             "lot_size_source": ["hkex", "hkex"], "lot_size_as_of": ["2026-09-09", "2026-09-09"],
                             "currency": ["HKD", "HKD"]})
    monkeypatch.setattr(hkex_instruments, "fetch_hkex_lot_sizes", lambda: official)
    result = hk_data_adapter.sync_hk_lot_sizes(tmp_path)
    saved = pl.read_parquet(path)
    assert saved["symbol"].to_list() == ["00001.HK", "00002.HK"]
    assert saved["custom_column"].to_list() == ["keep me", "keep me"]
    assert saved["lot_size"].to_list() == [500, None]
    assert result["status"] == "completed_with_errors"
    assert not (tmp_path / ".matrix_generation_us.json").exists()


def test_download_checkpoint_persists_to_indicator_readable_layout(tmp_path, monkeypatch):
    from app.data_providers import custom
    from app.services import kline_sync
    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo.db = SimpleNamespace(execute=lambda *args, **kwargs: None)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fixture")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *args: True)
    monkeypatch.setattr(custom, "get_provider", lambda name: SimpleNamespace(get_daily=lambda *args, **kwargs: _bars("BRK.A.US")))
    result = market_daily_sync.run_market_daily_sync(
        repo=repo, capset=CapabilitySet(), job_id="sample", market="US", symbols=["BRK.A.US", "MISSING.US"],
        start_date=datetime(2026, 1, 1), end_date=datetime(2026, 1, 10), compute_indicators=True,
    )
    assert result["succeeded"] == 1
    assert result["failed"] == 1
    assert result["enriched_dates_written"] == 1
    assert result["completed_symbols"] == ["BRK.A.US"]
    assert (tmp_path / "kline_hk_us_enriched" / "symbol=BRK.A.US" / "part.parquet").exists()
    assert not list((tmp_path / "kline_daily").glob("date=*"))


def test_market_data_api_rejects_mixed_input_and_reports_unsupported(tmp_path, monkeypatch):
    _without_market_daily(monkeypatch)
    from app.api import hk, us
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(market_daily_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    app = FastAPI()
    app.include_router(hk.router)
    app.include_router(us.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.capabilities = CapabilitySet()
    client = TestClient(app)
    assert client.post("/api/hk/enriched/sync?symbols=AAPL.US").status_code == 400
    assert client.post("/api/us/daily/sync?symbols=00700.HK").status_code == 400
    response = client.post("/api/us/daily/sync?symbols=BRK.A.US").json()
    assert response["status"] == "unsupported"
    assert response["items"][0]["symbol"] == "BRK.A.US"
    assert response["succeeded"] == 0
    assert client.post("/api/hk/enriched/sync").json()["status"] == "empty"
    assert client.get("/api/us/data/status").json()["market"] == "US"


def _without_market_daily(monkeypatch):
    from app.data_providers import registry
    monkeypatch.setattr(registry, "get_default_provider", lambda market, **kwargs: SimpleNamespace(
        name="fixture_no_daily", capabilities=SimpleNamespace(daily=False),
    ))


def test_us_download_uses_registered_market_source_without_cn_capability(tmp_path, monkeypatch):
    from app.data_providers import registry
    from app.services import kline_sync
    calls = []

    def fetch(symbols, start_time, end_time, asset_type):
        calls.append((symbols, start_time, end_time, asset_type))
        return _bars("BRK.A.US")

    provider = SimpleNamespace(name="us_fixture", capabilities=SimpleNamespace(daily=True), get_daily=fetch)
    monkeypatch.setattr(registry, "get_default_provider", lambda market: provider)
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo.db = SimpleNamespace(execute=lambda *args, **kwargs: None)
    succeeded = []
    count = kline_sync.sync_and_persist_daily_batch(
        ["BRK.A.US"], repo, CapabilitySet(), asset_type="us",
        start_date=datetime(2026, 1, 1), end_date=datetime(2026, 1, 10), successful_out=succeeded,
    )
    assert count == 2 and succeeded == ["BRK.A.US"]
    assert calls == [(["BRK.A.US"], datetime(2026, 1, 1), datetime(2026, 1, 10), "stock")]
    assert read_market_daily_symbol(tmp_path, "BRK.A.US")["close"].to_list() == [10.0, 11.0]
    status = market_data_status.get_market_data_status(tmp_path, "US", CapabilitySet())
    assert status["capabilities"]["daily_download"]
    assert status["capabilities"]["daily_provider"] == "us_fixture"


def test_hk_daily_capability_does_not_borrow_cn_permissions(monkeypatch):
    from app.services import kline_sync
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "tickflow")
    provider, supported, reason = kline_sync.daily_sync_capability(SimpleNamespace(has=lambda cap: True), "HK")
    assert provider == "hk_daily" and supported
    assert reason is None


def test_price_and_amount_provenance_survives_indicator_recompute(tmp_path):
    raw = _bars("AAPL.US").with_columns(
        pl.lit("fixture").alias("source"), pl.lit("unadjusted").alias("price_adjustment"),
        pl.lit("estimated_close_volume").alias("amount_source"),
        pl.lit("shares").alias("volume_unit"), pl.lit("USD").alias("currency"),
    )
    write_market_daily_symbol(tmp_path, "AAPL.US", raw)
    assert hk_data_adapter.sync_hk_daily_to_enriched("AAPL.US", tmp_path) == 1
    enriched = pl.read_parquet(tmp_path / "kline_hk_us_enriched" / "symbol=AAPL.US" / "part.parquet")
    assert enriched["price_adjustment"].unique().to_list() == ["unadjusted"]
    assert enriched["amount_source"].unique().to_list() == ["estimated_close_volume"]
    assert enriched["volume_unit"].unique().to_list() == ["shares"]
    assert enriched["close"].to_list() == raw["close"].to_list()
