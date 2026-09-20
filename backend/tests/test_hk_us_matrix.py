from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener as screener_api
from app.api import strategy as strategy_api
from app.backtest.engine import BacktestEngine
from app.backtest.matrix import load_market_data_matrix_from_parquet, matrix_feature
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.services import strategy_cache
from app.services.screener import ScreenerService
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository, enriched_dirname


def _write_symbol(root: Path, symbol: str, dates: list[date], *, falling: bool = False) -> Path:
    prices = [30.0 - i * 0.1 if falling else 10.0 + i * 0.2 for i in range(len(dates))]
    frame = pl.DataFrame({
        "symbol": [symbol] * len(dates),
        "date": dates,
        "open": prices,
        "high": [value + 0.5 for value in prices],
        "low": [value - 0.5 for value in prices],
        "close": prices,
        "raw_close": prices,
        "raw_high": [value + 0.5 for value in prices],
        "raw_low": [value - 0.5 for value in prices],
        "volume": [1000.0] * len(dates),
        "amount": [value * 1000.0 for value in prices],
        "change_pct": [0.02] * len(dates),
    })
    path = root / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return path


@pytest.fixture
def market_repo(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    # Sparse observations prove that warmup counts bars rather than calendar days.
    dates = [date(2024, 1, 5) + timedelta(weeks=i) for i in range(70)]
    for symbol in ("00700.HK", "AAPL.US", "BRK.A.US"):
        _write_symbol(tmp_path, symbol, dates)
    _write_symbol(tmp_path, "00941.HK", dates, falling=True)
    _write_symbol(tmp_path, "MSFT.US", [*dates, dates[-1] + timedelta(days=3)])
    for market, symbols in (("hk", ["00700.HK", "00941.HK"]), ("us", ["AAPL.US", "BRK.A.US", "MSFT.US"])):
        pl.DataFrame({
            "symbol": symbols, "name": [f"name {symbol}" for symbol in symbols],
            "lot_size": [500 if market == "hk" else 1] * len(symbols),
            "currency": ["HKD" if market == "hk" else "USD"] * len(symbols),
            "lot_size_status": ["verified_snapshot"] * len(symbols),
        }).write_parquet(
            tmp_path / "instruments" / f"{market}_instruments.parquet"
        )
    repo._enriched_cache = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2030, 1, 1)]})
    repo._enriched_cache_date = date(2030, 1, 1)
    yield repo, dates
    repo.db.close()


@pytest.mark.parametrize("asset_type, directory", [
    ("stock", "kline_daily_enriched"),
    ("etf", "kline_etf_enriched"),
    ("index", "kline_index_enriched"),
    ("hk", "kline_hk_us_enriched"),
    ("us", "kline_hk_us_enriched"),
])
def test_enriched_directory_has_explicit_asset_mapping(asset_type, directory):
    assert enriched_dirname(asset_type) == directory


def test_unknown_asset_does_not_fall_back_to_a_shares():
    with pytest.raises(ValueError, match="asset_type"):
        enriched_dirname("unknown-market")


def test_instrument_refresh_preserves_market_schema_and_nullable_fields(tmp_path):
    store = DataStore(tmp_path)
    instruments = tmp_path / "instruments"
    pl.DataFrame({"symbol": ["00700.HK"], "region": [None], "listing_date": [None],
                  "lot_size": [100], "currency": ["HKD"]}).write_parquet(instruments / "hk_instruments.parquet")
    pl.DataFrame({"symbol": ["600000.SH"], "region": ["Shanghai"],
                  "listing_date": ["1999-11-10"]}).write_parquet(instruments / "instruments.parquet")
    pl.DataFrame({"symbol": ["AAPL.US"], "region": ["US"],
                  "listing_date": [date(1980, 12, 12)]}).write_parquet(instruments / "us_instruments.parquet")
    repo = KlineRepository(store)
    try:
        result = repo.get_instruments()
        assert set(result["symbol"]) == {"00700.HK", "600000.SH", "AAPL.US"}
        hk = result.filter(pl.col("symbol") == "00700.HK").to_dicts()[0]
        assert hk["region"] is None and hk["lot_size"] == 100 and hk["currency"] == "HKD"
        assert result.filter(pl.col("symbol") == "600000.SH")["region"].item() == "Shanghai"
        assert result.filter(pl.col("symbol") == "AAPL.US")["listing_date"].item() == "1980-12-12"
    finally:
        repo.db.close()


@pytest.mark.parametrize("asset_types", [["stock"], ["stock", "etf"]])
def test_existing_composite_keeps_its_scope_when_builtins_gain_foreign_markets(tmp_path, asset_types):
    composite_dir = tmp_path / "strategies" / "composite"
    composite_dir.mkdir(parents=True)
    meta = {
        "id": "legacy_cn_mix",
        "name": "Legacy CN mix",
        "asset_types": asset_types,
        "timeframes": ["1d"],
        "children": [
            {"strategy_id": strategy_id, "weight": 1.0}
            for strategy_id in ("ma_golden_cross", "macd_golden", "boll_breakout", "trend_breakout")
        ],
    }
    (composite_dir / "legacy_cn_mix.py").write_text(
        f'META = {meta!r}\nEXECUTION_BACKEND = "composite"\n', encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[
        Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin", composite_dir,
    ])
    assert engine.load_errors() == []
    composite = engine.get("legacy_cn_mix")
    assert composite.meta["asset_types"] == asset_types
    for asset_type in asset_types:
        engine.validate_context(
            composite, StrategyDataContext(asset_type=asset_type, timeframe="1d", as_of=date(2024, 1, 5)),
        )
    for market in ("hk", "us"):
        with pytest.raises(ValueError, match="does not support asset_type"):
            engine.validate_context(
                composite, StrategyDataContext(asset_type=market, timeframe="1d", as_of=date(2024, 1, 5)),
            )


@pytest.mark.parametrize("market", ["hk", "us"])
def test_latest_snapshot_is_market_scoped_and_refreshes_after_write(market_repo, market):
    repo, dates = market_repo
    snapshot, latest = repo.get_enriched_latest_asset(market)
    expected_date = dates[-1] if market == "hk" else dates[-1] + timedelta(days=3)
    assert latest == expected_date
    assert all(symbol.endswith(f".{market.upper()}") for symbol in snapshot["symbol"])
    assert snapshot["date"].unique().to_list() == [expected_date]
    assert "name" in snapshot.columns
    assert "change_pct" in snapshot.columns
    cached, _ = repo.get_enriched_latest_asset(market)
    assert cached is snapshot

    symbol = "00700.HK" if market == "hk" else "AAPL.US"
    next_date = expected_date + timedelta(days=1)
    _write_symbol(repo.store.data_dir, symbol, [*dates, next_date])
    updated, latest = repo.get_enriched_latest_asset(market)
    assert latest == next_date
    assert updated["symbol"].to_list() == [symbol]
    assert repo.get_enriched_latest()[1] == date(2030, 1, 1)


def test_foreign_refresh_false_does_not_touch_disk(market_repo, monkeypatch):
    repo, _ = market_repo
    snapshot, latest = repo.get_enriched_latest_asset("hk")

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("refresh=False must not scan source files")

    monkeypatch.setattr(repo, "get_matrix_data_generation", unexpected_scan)
    cached, cached_date = repo.get_enriched_latest_asset("hk", refresh=False)
    assert cached is snapshot
    assert cached_date == latest
    assert repo.get_enriched_latest_asset("us", refresh=False)[0].is_empty()
    repo.clear_cache()
    assert repo.get_enriched_latest_asset("hk", refresh=False)[0].is_empty()


@pytest.mark.parametrize("market", ["hk", "us"])
def test_screener_history_keeps_full_trading_window_and_no_future_rows(market_repo, market):
    repo, dates = market_repo
    service = ScreenerService(repo, asset_type=market)
    frame = service._load_enriched_history(dates[-2], 61)
    assert frame["date"].unique().sort().to_list() == dates[-62:-1]
    assert all(symbol.endswith(f".{market.upper()}") for symbol in frame["symbol"])
    assert "name" in frame.columns
    assert "change_pct" in frame.columns
    current = service._load_enriched_for_date(dates[-2])
    assert current["date"].unique().to_list() == [dates[-2]]


def test_market_generation_tracks_legacy_writes_without_cross_market_invalidation(market_repo):
    repo, dates = market_repo
    hk_before = repo.get_matrix_data_generation("hk")
    us_before = repo.get_matrix_data_generation("us")
    _write_symbol(repo.store.data_dir, "00700.HK", [*dates, dates[-1] + timedelta(days=1)])
    assert repo.get_matrix_data_generation("hk") != hk_before
    assert repo.get_matrix_data_generation("us") == us_before


@pytest.mark.parametrize("market", ["hk", "us"])
def test_backtest_matrix_reads_only_requested_market(market_repo, market, monkeypatch):
    from app.backtest.engine import settings

    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", False)
    repo, dates = market_repo
    plan = SimpleNamespace(
        execution_backend="matrix_native",
        base_columns={"symbol", "date", "open", "high", "low", "close", "volume"},
        instrument_columns=set(),
        matrix_columns={"amount"},
    )
    matrix = BacktestEngine(repo).load_market_data_matrix_for_backtest(
        None, dates[0], dates[-1], plan, asset_type=market,
    )
    assert len(matrix.timestamp_labels) == len(dates)
    assert all(symbol.endswith(f".{market.upper()}") for symbol in matrix.symbols)
    assert all(name.startswith("name ") for name in matrix.names)
    symbol = "00700.HK" if market == "hk" else "AAPL.US"
    column = matrix.symbols.index(symbol)
    np.testing.assert_allclose(matrix.close[:, column], 10.0 + np.arange(70) * 0.2, rtol=1e-6)
    assert matrix_feature(matrix, "ma60")[-1, column] == pytest.approx(17.9)
    panel = BacktestEngine(repo).load_panel(None, dates[-2], dates[-1], asset_type=market)
    assert all(value.endswith(f".{market.upper()}") for value in panel["symbol"])


def test_symbol_partition_disk_cache_reuses_and_invalidates_on_append_and_rewrite(tmp_path):
    dates = [date(2024, 1, 5) + timedelta(days=i) for i in range(5)]
    path = _write_symbol(tmp_path, "00700.HK", dates)
    root = path.parent.parent
    cache = tmp_path / "matrix-cache"

    def load(start=dates[0], end=dates[-1]):
        return load_market_data_matrix_from_parquet(
            root, start, end, field_columns={"amount"}, cache_root=cache,
        )

    first = load()
    assert first.cache_status == "built"
    assert load().cache_status == "exact"
    assert load(dates[1], dates[-2]).cache_status == "covering"
    _write_symbol(tmp_path, "00941.HK", dates, falling=True)
    appended = load()
    assert appended.cache_status == "built"
    assert set(appended.symbols) == {"00700.HK", "00941.HK"}
    pl.read_parquet(path).with_columns((pl.col("close") + 2.0).alias("close")).write_parquet(path)
    rewritten = load()
    assert rewritten.cache_status == "built"
    assert rewritten.close[0, rewritten.symbols.index("00700.HK")] == pytest.approx(12.0)
    path.unlink()
    removed = load()
    assert removed.symbols == ("00941.HK",)
    assert removed.cache_status == "built"


@pytest.mark.parametrize("statistics", [True, False])
def test_symbol_partition_timestamp_dates_and_missing_statistics(tmp_path, statistics):
    dates = [date(2024, 1, 5), date(2024, 1, 8)]
    path = _write_symbol(tmp_path, "BRK.A.US", dates)
    pl.read_parquet(path).with_columns(pl.col("date").cast(pl.Datetime)).write_parquet(
        path, statistics=statistics,
    )
    matrix = load_market_data_matrix_from_parquet(
        path.parent.parent, dates[0], dates[-1], field_columns={"amount"},
    )
    assert matrix.symbols == ("BRK.A.US",)
    assert matrix.timestamp_labels == tuple(day.isoformat() for day in dates)


@pytest.mark.parametrize("market", ["hk", "us"])
def test_full_strategy_backtest_uses_foreign_matrix(market_repo, market, monkeypatch):
    from app.backtest.engine import settings

    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", False)
    repo, dates = market_repo
    strategies = StrategyEngine(
        strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"],
        data_dir=repo.store.data_dir,
    )
    config = StrategyBacktestConfig(
        strategy_id="ma_golden_cross", symbols=None, start=dates[0], end=dates[-1],
        asset_type=market,
        params={"require_ma_golden": False, "use_volume_filter": False},
        overrides={"basic_filter": {"amount_min": 0}},
    )
    result = StrategyBacktestService(BacktestEngine(repo), strategies).run(
        config, result_policy=BacktestResultPolicy(include_monte_carlo=False, include_benchmark=False),
    )
    assert result.error is None
    assert result.trades
    assert all(trade["symbol"].endswith(f".{market.upper()}") for trade in result.trades)
    assert all(trade["entry_date"] <= str(dates[-1]) for trade in result.trades)
    assert result.config["market"] == market.upper()
    assert result.config["currency"] == ("HKD" if market == "hk" else "USD")
    assert result.config["data_generation"]
    assert result.config["benchmark_available"] is False
    assert all(trade["shares"] % (500 if market == "hk" else 1) == 0 for trade in result.trades)


@pytest.mark.parametrize("basic_filter", [{"pe_ttm_min": 0}, {"pb_max": 10}])
def test_foreign_backtest_rejects_current_valuations_as_history(market_repo, basic_filter):
    repo, dates = market_repo
    strategies = StrategyEngine(
        strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"],
        data_dir=repo.store.data_dir,
    )
    result = StrategyBacktestService(BacktestEngine(repo), strategies).run(StrategyBacktestConfig(
        strategy_id="ma_golden_cross", symbols=["AAPL.US"], start=dates[0], end=dates[-1],
        asset_type="us", overrides={"basic_filter": basic_filter},
    ))
    assert result.error and "可追溯" in result.error
    assert result.trades == []


@pytest.mark.parametrize("market,symbol", [("hk", "00700.HK"), ("us", "AAPL.US")])
def test_market_publication_updates_snapshot_and_blocks_incomplete_reads(market_repo, market, symbol):
    from app.enriched_generation import EnrichedGenerationUnavailableError, EnrichedPublication
    from app.services.hk_data_adapter import publish_hk_daily_snapshot, sync_hk_daily_to_enriched

    repo, dates = market_repo
    repo.get_enriched_latest_asset(market)
    part = repo.store.data_dir / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    raw = repo.store.data_dir / "kline_daily" / f"symbol={symbol}" / "part.parquet"
    raw.parent.mkdir(parents=True, exist_ok=True)
    before = repo.get_matrix_data_generation(market)
    updated = pl.read_parquet(part).with_columns(
        pl.lit(42.0).alias("open"), pl.lit(42.5).alias("high"),
        pl.lit(41.5).alias("low"), pl.lit(42.0).alias("close"),
    )
    if market == "hk":
        updated = updated.select("symbol", "date", "open", "high", "low", "close", "volume", "amount").with_columns(
            pl.lit("fixture").alias("source"), pl.lit("unadjusted").alias("price_adjustment"),
            pl.lit(1).alias("price_schema_version"), pl.lit(True).alias("raw_price_verified"),
            pl.lit("share").alias("volume_unit"), pl.lit("HKD").alias("currency"),
        )
        factors = pl.DataFrame({"symbol": [symbol], "trade_date": [date(1900, 1, 1)], "ex_factor": [1.0],
                                "source": ["fixture_actions"], "version": ["fixture-v1"], "coverage_end": [dates[-1]]})
        assert publish_hk_daily_snapshot(repo.store.data_dir, symbol, updated, factors=factors)["enriched_updated"]
    else:
        updated.write_parquet(raw)
        assert sync_hk_daily_to_enriched(symbol, repo.store.data_dir) == 1
    assert repo.get_matrix_data_generation(market) != before
    frame = repo.read_market_enriched(market, start=dates[-1], end=dates[-1])
    assert frame.filter(pl.col("symbol") == symbol)["close"].item() == pytest.approx(42.0)

    publication = EnrichedPublication(repo.store.data_dir, market)
    publication.write_parquet(pl.read_parquet(part), part)
    with pytest.raises(EnrichedGenerationUnavailableError, match=r"being published|正在发布"):
        repo.get_enriched_latest_asset(market)
    publication.commit()
    assert repo.get_enriched_latest_asset(market)[1] is not None


@pytest.mark.parametrize("market", ["hk", "us"])
def test_matrix_screener_api_uses_market_history_and_preserves_a_share_cache(market_repo, market):
    repo, dates = market_repo
    engine = StrategyEngine(
        strategy_dirs=[Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"],
        data_dir=repo.store.data_dir,
    )
    strategy_config.save_override(repo.store.data_dir, "ma_golden_cross", {
        "params": {"require_ma_golden": False, "use_volume_filter": False},
        "basic_filter": {"amount_min": 0},
    })
    strategy_cache.write_cache(repo.store.data_dir, str(dates[-1]), {
        "ma_golden_cross": {"total": 1, "as_of": str(dates[-1]), "rows": [{"symbol": "600000.SH"}]},
    })
    original_cache = strategy_cache.read_cache(repo.store.data_dir)
    app = FastAPI()
    app.state.repo = repo
    app.state.strategy_engine = engine
    app.include_router(screener_api.router)
    app.include_router(strategy_api.router)
    client = TestClient(app)

    listed = client.get("/api/strategies", params={"asset_type": market}).json()["strategies"]
    assert "ma_golden_cross" in {item["id"] for item in listed}
    assert "consecutive_limit_ups" not in {item["id"] for item in listed}
    response = client.post("/api/screener/run_preset", json={
        "strategy_id": "ma_golden_cross", "asset_type": market, "as_of": str(dates[-1]),
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["as_of"] == str(dates[-1])
    expected = {"00700.HK"} if market == "hk" else {"AAPL.US", "BRK.A.US", "MSFT.US"}
    assert {row["symbol"] for row in result["rows"]} == expected
    assert strategy_cache.read_cache(repo.store.data_dir) == original_cache
    latest_response = client.post("/api/screener/run_preset", json={
        "strategy_id": "ma_golden_cross", "asset_type": market,
    })
    assert latest_response.status_code == 200, latest_response.text
    expected_latest = dates[-1] if market == "hk" else dates[-1] + timedelta(days=3)
    assert latest_response.json()["as_of"] == str(expected_latest)
    batch = client.post("/api/screener/run_all", json={
        "strategy_ids": ["ma_golden_cross"], "asset_type": market, "as_of": str(dates[-1]),
    })
    assert batch.status_code == 200, batch.text
    assert batch.json()["results"]["ma_golden_cross"]["total"] == len(expected)
    assert strategy_cache.read_cache(repo.store.data_dir) == original_cache
    invalid = client.post("/api/screener/run_preset", json={
        "strategy_id": "consecutive_limit_ups", "asset_type": market,
    })
    assert invalid.status_code == 400
    assert client.post("/api/screener/run_preset", json={
        "strategy_id": "ma_golden_cross", "asset_type": "unknown-market",
    }).status_code == 422
    from app.enriched_generation import EnrichedPublication

    symbol = "00700.HK" if market == "hk" else "AAPL.US"
    part = repo.store.data_dir / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
    publication = EnrichedPublication(repo.store.data_dir, market)
    publication.write_parquet(pl.read_parquet(part), part)
    for as_of in (None, str(dates[-1])):
        busy = client.post("/api/screener/run_preset", json={
            "strategy_id": "ma_golden_cross", "asset_type": market, "as_of": as_of,
        })
        assert busy.status_code == 503
    publication.commit()


def test_missing_foreign_data_does_not_read_a_share_snapshot(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    try:
        repo._enriched_cache = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2024, 1, 5)]})
        repo._enriched_cache_date = date(2024, 1, 5)
        for market in ("hk", "us"):
            assert ScreenerService(repo, asset_type=market).latest_date() is None
            assert repo.get_enriched_latest_asset(market)[0].is_empty()
    finally:
        repo.db.close()
