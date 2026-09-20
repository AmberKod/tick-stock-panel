"""HK point-in-time fields and every matching path share the same guardrails."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.fundamentals import (
    attach_fundamental_factors,
    build_fundamental_matrices,
    load_fundamental_snapshot,
)
from app.backtest.matrix import build_market_data_matrix


def write_history(data_dir: Path):
    records = []
    for period, announce, revision, value in [
        ("2024-12-31", "2025-03-19", "a", 20),
        ("2025-06-30", "2025-08-13", "b", 30),
        ("2025-06-30", "2025-08-16", "c", 31),
        ("2024-12-31", "2025-08-20", "older_revision", 99),
    ]:
        records.append({"symbol": "00700.HK", "period_end": period, "announce_date": announce,
                        "revision_id": revision, "source": "test_disclosure", "report_currency": "CNY",
                        "publication_source": "issuer", "source_url": "https://example.com/report",
                        "field_provenance": json.dumps({"gross_margin": {"source": "test_disclosure", "unit": "percent_number", "currency": "CNY", "announce_date": announce, "basis": "as_reported"}}),
                        "gross_margin": float(value)})
    directory = data_dir / "financials" / "metrics"
    directory.mkdir(parents=True)
    pl.DataFrame(records).write_parquet(directory / "hk.parquet")


def panel():
    days = [date(2025, 3, 19), date(2025, 3, 20), date(2025, 8, 13), date(2025, 8, 14), date(2025, 8, 18), date(2025, 8, 21)]
    return pl.DataFrame({"symbol": ["00700.HK"] * len(days), "date": days, "open": [10.0] * len(days),
                         "close": [10.0] * len(days), "high": [11.0] * len(days), "low": [9.0] * len(days), "volume": [1000.0] * len(days)})


def test_hk_announcement_day_keeps_previous_period_and_revisions_never_leak(tmp_path):
    write_history(tmp_path)
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["gross_margin_latest"])
    assert snapshot is not None
    result = attach_fundamental_factors(panel(), snapshot, ["gross_margin_latest"])
    assert result["gross_margin_latest"].to_list() == [None, 20, 20, 30, 31, 31]


def test_hk_matrix_and_panel_equal_for_announcement_and_weekend_revision(tmp_path):
    write_history(tmp_path)
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["gross_margin_latest", "roe_latest"])
    data = panel()
    values = attach_fundamental_factors(data, snapshot, ["gross_margin_latest", "roe_latest"])
    market = build_market_data_matrix(data)
    matrices = build_fundamental_matrices(market, snapshot, ["gross_margin_latest", "roe_latest"])
    for name in ("gross_margin_latest", "roe_latest"):
        np.testing.assert_allclose(matrices[name][:, 0], values[name].to_numpy(), equal_nan=True)
    assert values["roe_latest"].null_count() == values.height


def test_hk_late_field_provenance_cannot_inherit_an_early_row_date(tmp_path):
    write_history(tmp_path)
    path = tmp_path / "financials" / "metrics" / "hk.parquet"
    rows = pl.read_parquet(path).to_dicts()[:1]
    provenance = json.loads(rows[0]["field_provenance"])
    provenance["gross_margin"]["announce_date"] = "2025-08-13"
    rows[0]["field_provenance"] = json.dumps(provenance)
    pl.DataFrame(rows).write_parquet(path)
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["gross_margin_latest"])
    result = attach_fundamental_factors(panel(), snapshot, ["gross_margin_latest"])
    assert result["gross_margin_latest"].to_list()[:4] == [None, None, None, 20]


def test_new_report_missing_field_does_not_reuse_previous_period(tmp_path):
    write_history(tmp_path)
    path = tmp_path / "financials" / "metrics" / "hk.parquet"
    rows = pl.read_parquet(path).head(3).to_dicts()
    rows[1]["gross_margin"] = None
    rows[1]["net_margin"] = 25.0
    rows[1]["field_provenance"] = json.dumps({"net_margin": {
        "source": "test_disclosure", "unit": "percent_number", "currency": "CNY",
        "announce_date": rows[1]["announce_date"], "basis": "as_reported",
    }})
    pl.DataFrame(rows).write_parquet(path)
    requested = ["gross_margin_latest", "net_margin_latest"]
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=requested)
    values = attach_fundamental_factors(panel(), snapshot, requested)
    assert values["gross_margin_latest"].to_list() == [None, 20, 20, None, 31, 31]
    # A later version of the same reporting period may fill a field without
    # discarding another verified field already published for that period.
    assert values["net_margin_latest"].to_list() == [None, None, None, 25, 25, 25]
    matrices = build_fundamental_matrices(build_market_data_matrix(panel()), snapshot, requested)
    for field in requested:
        np.testing.assert_allclose(matrices[field][:, 0], values[field].to_numpy(), equal_nan=True)


@pytest.mark.parametrize("currency,verified", [("HKD", True), ("CNY", True), ("HKD", False), ("HKD", None)])
def test_hk_per_share_valuation_matches_matrix_and_needs_valid_price_basis(tmp_path, currency, verified):
    write_history(tmp_path)
    path = tmp_path / "financials" / "metrics" / "hk.parquet"
    row = pl.read_parquet(path).head(1).to_dicts()[0]
    provenance = json.loads(row["field_provenance"])
    for field, value in (("bps", 2.0), ("eps_ttm", 1.0)):
        row[field] = value
        provenance[field] = {
            "source": "test_disclosure", "unit": "currency_per_share", "currency": "HKD",
            "announce_date": "2025-03-19", "basis": "as_reported",
            "per_share_basis": {"verified": True, "valid_from": "2025-03-20", "valid_to": "2025-08-17"},
        }
    row["field_provenance"] = json.dumps(provenance)
    pl.DataFrame([row]).write_parquet(path)
    requested = ["pb_latest", "pb", "raw_pb", "pe_ttm"]
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=requested)
    data = panel().with_columns(pl.lit(20.0).alias("raw_close"), pl.lit(currency).alias("currency"),
                                pl.lit(verified, dtype=pl.Boolean).alias("raw_price_verified"))
    ordinary = attach_fundamental_factors(data, snapshot, requested)
    matrix = build_market_data_matrix(data)
    result = build_fundamental_matrices(matrix, snapshot, requested)
    for name in requested:
        np.testing.assert_allclose(result[name][:, 0], ordinary[name].to_numpy(), equal_nan=True)
    if currency == "HKD" and verified is True:
        assert ordinary["pb"].to_list() == [None, 10, 10, 10, None, None]
        assert ordinary["pe_ttm"].to_list() == [None, 20, 20, 20, None, None]
    else:
        assert ordinary["pb"].null_count() == ordinary.height


def test_hk_turnover_uses_only_dated_float_shares_and_share_units(tmp_path):
    write_history(tmp_path)
    path = tmp_path / "financials" / "metrics" / "hk.parquet"
    row = pl.read_parquet(path).head(1).to_dicts()[0]
    provenance = json.loads(row["field_provenance"])
    provenance["float_shares"] = {
        "source": "test_disclosure", "unit": "share", "announce_date": "2025-03-19", "basis": "as_reported",
        "per_share_basis": {"verified": True, "valid_from": "2025-03-20", "valid_to": "2025-08-17"},
    }
    row.update(float_shares=10000.0, field_provenance=json.dumps(provenance))
    pl.DataFrame([row]).write_parquet(path)
    snapshot = load_fundamental_snapshot(tmp_path, market="HK", names=["turnover_rate"])
    for unit in ("share", "lot", None):
        data = panel().with_columns(pl.lit(unit, dtype=pl.String).alias("volume_unit"), pl.lit(123.0).alias("turnover_rate"))
        ordinary = attach_fundamental_factors(data, snapshot, ["turnover_rate"])
        matrix = build_fundamental_matrices(build_market_data_matrix(data), snapshot, ["turnover_rate"])
        np.testing.assert_allclose(matrix["turnover_rate"][:, 0], ordinary["turnover_rate"].to_numpy(), equal_nan=True)
        assert ordinary["turnover_rate"].to_list() == ([None, 10, 10, 10, None, None] if unit == "share" else [None] * 6)


class FinancialEntries:
    def required_fields(self):
        return frozenset({"close", "gross_margin_latest"})

    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        from app.backtest.matrix import make_signal_matrix

        return make_signal_matrix(market.shape, entry=(market.fields["gross_margin_latest"] >= 15).astype(np.uint8))


@pytest.fixture
def financial_project(tmp_path, monkeypatch):
    from app.backtest.engine import settings
    from app.strategy.engine import CompositeChild, CompositeSpec, StrategyDef, StrategyEngine
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", False)
    write_history(tmp_path)
    path = tmp_path / "financials" / "metrics" / "hk.parquet"
    history = pl.read_parquet(path)
    pl.concat([history, history.with_columns(pl.lit("00005.HK").alias("symbol"), (pl.col("gross_margin") / 2).alias("gross_margin"))]).write_parquet(path)
    repo = KlineRepository(DataStore(tmp_path))
    for symbol in ("00700.HK", "00005.HK"):
        data = panel().with_columns(
            pl.lit(symbol).alias("symbol"), pl.lit(20.0).alias("raw_close"),
            pl.lit(22.0).alias("raw_high"), pl.lit(18.0).alias("raw_low"),
            pl.lit(20000.0).alias("amount"),
            pl.lit("HKD").alias("currency"), pl.lit(True).alias("raw_price_verified"),
            pl.lit("share").alias("volume_unit"), pl.lit("fixture_daily").alias("source"),
            pl.lit(1).alias("price_schema_version"), pl.lit("forward_adjusted").alias("price_adjustment"),
            pl.lit("fixture_actions").alias("adjustment_source"), pl.lit("fixture-v1").alias("adjustment_version"),
            pl.lit(date(2025, 8, 21)).alias("adjustment_as_of"),
        )
        destination = tmp_path / "kline_hk_us_enriched" / f"symbol={symbol}" / "part.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        data.write_parquet(destination)
    pl.DataFrame({"symbol": ["00700.HK", "00005.HK"], "name": ["Tencent fixture", "HSBC fixture"],
                  "currency": ["HKD", "HKD"], "lot_size": [100, 400], "lot_size_status": ["verified_snapshot"] * 2,
                  "lot_size_as_of": [date(2025, 1, 1)] * 2}).write_parquet(tmp_path / "instruments" / "hk_instruments.parquet")
    engine = StrategyEngine(strategy_dirs=[], data_dir=tmp_path)
    ordinary = StrategyDef(
        meta={"id": "financial_ordinary", "name": "financial fixture", "asset_types": ["hk"], "timeframes": ["1d"],
              "scoring": {"gross_margin_latest": 1.0}, "order_by": "score"},
        basic_filter={"enabled": False}, entry_signals=[], exit_signals=[], stop_loss=None,
        trailing_stop=None, trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
        max_hold_days=1, filter_fn=lambda frame, params: pl.col("gross_margin_latest") >= 15,
        filter_history_fn=None, lookback_days=1, source="custom", required_features=frozenset({"gross_margin_latest"}),
    )
    engine._strategies["financial_ordinary"] = ordinary
    engine._strategies["financial_native"] = replace(ordinary, meta={**ordinary.meta, "id": "financial_native"},
        filter_fn=None, execution_backend="matrix_native", matrix_strategy=FinancialEntries())
    engine._strategies["financial_blend"] = replace(ordinary, meta={**ordinary.meta, "id": "financial_blend", "params": []},
        filter_fn=None, execution_backend="composite", composite=CompositeSpec((CompositeChild("financial_ordinary", 1.0), CompositeChild("financial_native", 1.0))))
    yield repo, engine
    repo.db.close()


def test_financial_repository_screen_matrix_shared_and_composite_agree(financial_project):
    from app.services.screener import ScreenerService

    repo, engine = financial_project
    service = ScreenerService(repo, asset_type="hk")
    context = service.build_strategy_context(engine, date(2025, 8, 14), ["financial_ordinary", "financial_native"])
    ordinary = engine.run("financial_ordinary", context)
    native = engine.run("financial_native", context)
    shared = engine.run("financial_native", replace(context, market=build_market_data_matrix(context.history)))
    blended = engine.run("financial_blend", context)
    assert ordinary.total == 2
    assert ordinary.scores == pytest.approx(native.scores)
    assert ordinary.scores == pytest.approx(shared.scores)
    assert set(blended.scores) == set(ordinary.scores)
    values = {row["symbol"]: row["gross_margin_latest"] for row in ordinary.rows}
    assert values == {"00700.HK": 30.0, "00005.HK": 15.0}
    projected = repo.read_market_enriched("hk", start=date(2025, 8, 13), end=date(2025, 8, 14), columns=["gross_margin_latest"])
    assert projected.filter(pl.col("symbol") == "00700.HK")["gross_margin_latest"].to_list() == [20.0, 30.0]


def test_financial_backtest_and_prepared_matrix_preserve_values_and_assumptions(financial_project):
    from app.backtest.strategy import (
        BacktestResultPolicy,
        StrategyBacktestConfig,
        StrategyBacktestService,
    )

    repo, strategies = financial_project
    service = StrategyBacktestService(BacktestEngine(repo), strategies)
    cfg = StrategyBacktestConfig(strategy_id="financial_native", asset_type="hk", symbols=None,
                                 start=date(2025, 8, 13), end=date(2025, 8, 18), mode="full", holding_days=1)
    policy = BacktestResultPolicy(include_monte_carlo=False, include_benchmark=False)
    native = service.run(cfg, result_policy=policy)
    ordinary = service.run(replace(cfg, strategy_id="financial_ordinary"), result_policy=policy)
    prepared = service.prepare_matrix_optimization([cfg])
    shared = service.run(cfg, prepared=prepared, result_policy=policy)
    for result in (native, ordinary, shared):
        assert result.error is None
        assert result.trades
        assumptions = result.config["execution_assumptions"]
        assert assumptions["financial_fields"] == ["gross_margin_latest"]
        assert assumptions["financial_sources"] == ["test_disclosure"]
        assert assumptions["price_sources"] == ["fixture_daily"]
        assert assumptions["adjustment_versions"] == ["fixture-v1"]
        assert assumptions["price_verified_range"]["verified_symbols"] == 2
        assert assumptions["lot_size_snapshot_dates"] == ["2025-01-01"]
        assert assumptions["corporate_actions_simulated"] is False
        assert assumptions["settlement_ledger_simulated"] is False
    def key(result):
        return sorted((trade["symbol"], trade["entry_date"], trade["entry_signal_date"]) for trade in result.trades)
    assert key(native) == key(ordinary) == key(shared)


def test_financial_publication_invalidates_hk_context_panel_matrix_and_prepared(financial_project):
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from app.enriched_generation import EnrichedGenerationUnavailableError, EnrichedPublication
    from app.services.market_data_status import market_data_generation
    from app.services.screener import ScreenerService

    repo, strategies = financial_project
    backtest = BacktestEngine(repo)
    service = StrategyBacktestService(backtest, strategies)
    cfg = StrategyBacktestConfig(strategy_id="financial_native", asset_type="hk", symbols=None,
                                 start=date(2025, 8, 13), end=date(2025, 8, 21))
    prepared = service.prepare_matrix_optimization([cfg])
    context = ScreenerService(repo, asset_type="hk").build_strategy_context(strategies, date(2025, 8, 21), [cfg.strategy_id])
    old_panel = backtest.load_panel(["00700.HK"], date(2025, 8, 21), date(2025, 8, 21), ["gross_margin_latest"], "hk")
    before = {market: repo.get_matrix_data_generation(market) for market in ("hk", "us", "stock")}
    path = repo.store.data_dir / "financials" / "metrics" / "hk.parquet"
    rows = pl.read_parquet(path).to_dicts()
    revised = dict(next(row for row in rows if row["symbol"] == "00700.HK" and row["revision_id"] == "b"))
    revised.update(announce_date="2025-08-19", revision_id="new_public_revision", gross_margin=40.0)
    provenance = json.loads(revised["field_provenance"])
    provenance["gross_margin"]["announce_date"] = "2025-08-19"
    revised["field_provenance"] = json.dumps(provenance)
    publication = EnrichedPublication(repo.store.data_dir, "hk")
    publication.write_parquet(pl.DataFrame([*rows, revised]), path)
    publication.commit()
    assert repo.get_matrix_data_generation("hk") != before["hk"]
    assert repo.get_matrix_data_generation("hk") == market_data_generation(repo.store.data_dir, "HK")
    assert all(repo.get_matrix_data_generation(market) == before[market] for market in ("stock", "us"))
    assert old_panel["gross_margin_latest"].item() == 31
    assert backtest.load_panel(["00700.HK"], date(2025, 8, 21), date(2025, 8, 21), ["gross_margin_latest"], "hk")["gross_margin_latest"].item() == 40
    with pytest.raises(EnrichedGenerationUnavailableError, match="版本已过期"):
        strategies.run(cfg.strategy_id, context)
    assert "版本已过期" in service.run(cfg, prepared=prepared).error
    with pytest.raises(ValueError, match="来源版本缺失或已过期"):
        service.prepare_matrix_optimization([cfg], market_data_override=prepared.market_data)


@pytest.mark.parametrize("disk_cache", [False, True])
def test_hk_shared_matrix_preserves_its_original_generation(financial_project, monkeypatch, disk_cache):
    from app.backtest.engine import settings
    from app.backtest.matrix import slice_market_data_matrix
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    repo, strategies = financial_project
    monkeypatch.setattr(settings, "backtest_matrix_disk_cache_enabled", disk_cache)
    service = StrategyBacktestService(BacktestEngine(repo), strategies)
    cfg = StrategyBacktestConfig(strategy_id="financial_native", asset_type="hk", symbols=None,
                                 start=date(2025, 8, 13), end=date(2025, 8, 21))
    prepared = service.prepare_matrix_optimization([cfg])
    expected = repo.get_matrix_data_generation("hk")
    assert prepared.data_generation == expected
    assert prepared.market_data.source_generation == expected
    assert slice_market_data_matrix(prepared.market_data, 1, 4).source_generation == expected
    repeated = service.prepare_matrix_optimization([cfg])
    assert repeated.market_data.source_generation == expected
    if disk_cache:
        assert repeated.market_data.cache_status in {"exact", "covering"}
    shared = service.prepare_matrix_optimization([cfg], market_data_override=prepared.market_data)
    assert shared.market_data.source_generation == expected


def test_old_matrix_cannot_be_stamped_with_the_current_generation(financial_project, monkeypatch):
    from app.backtest import engine as engine_module
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
    from app.enriched_generation import EnrichedGenerationUnavailableError

    repo, strategies = financial_project
    service = StrategyBacktestService(BacktestEngine(repo), strategies)
    cfg = StrategyBacktestConfig(strategy_id="financial_native", asset_type="hk", symbols=None,
                                 start=date(2025, 8, 13), end=date(2025, 8, 21))
    prepared = service.prepare_matrix_optimization([cfg])
    old_market = replace(prepared.market_data, source_generation="old-financial-generation")
    monkeypatch.setattr(engine_module, "load_market_data_matrix_from_parquet", lambda *args, **kwargs: old_market)
    with pytest.raises(EnrichedGenerationUnavailableError, match="来源版本"):
        service.prepare_matrix_optimization([cfg])
    assert old_market.source_generation == "old-financial-generation"
    inconsistent = replace(prepared, market_data=old_market)
    assert "版本已过期" in service.run(cfg, prepared=inconsistent).error


def test_hk_worker_rejects_a_task_queued_before_publication(tmp_path):
    from app.backtest.strategy import StrategyBacktestConfig
    from app.backtest.worker import BacktestWorkerError, make_worker_task, run_worker_task

    cfg = StrategyBacktestConfig(strategy_id="ma_golden_cross", asset_type="hk", symbols=["00700.HK"], start=date(2025, 8, 13), end=date(2025, 8, 21))
    task = make_worker_task("backtest", tmp_path, cfg)
    write_history(tmp_path)
    with pytest.raises(BacktestWorkerError, match="changed"):
        run_worker_task(task)


PATHS = ("portfolio", "portfolio_legacy", "independent_candidates", "independent_candidates_legacy")


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("currency,status,reason", [
    ("CNY", "verified_snapshot", "buy_currency_unsupported"),
    ("USD", "verified_snapshot", "buy_currency_unsupported"),
    (None, "verified_snapshot", "buy_currency_unknown"),
    ("HKD", "future_snapshot", "buy_lot_size_unverified"),
    ("HKD", "conflict", "buy_lot_size_unverified"),
])
def test_explicit_lot_cannot_bypass_hk_execution_metadata(path, currency, status, reason):
    class Repo:
        def get_instruments_asset(self, asset_type):
            assert asset_type == "hk"
            return pl.DataFrame({"symbol": ["00700.HK"], "lot_size": [100], "currency": [currency],
                                 "lot_size_status": [status], "lot_size_as_of": [date(2025, 1, 1)]})

    data = panel().head(3)
    entries = data["date"] == data["date"][0]
    exits = data["date"] == data["date"][1]
    cfg = MatcherConfig(asset_type="hk", matching="open_t+1", lot_sizes={"00700.HK": 1})
    result = getattr(BacktestEngine(Repo()), f"simulate_{path}")(data, entries, exits, cfg)
    assert result.trades == []
    assert result.stats["execution"][reason] == 1
    assert any(item["reason"] == reason for item in result.stats["execution_diagnostics"])


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("instrument_status", ["delisted", "temporary_counter_closed", "rights_trading_ended"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_inactive_instrument_guard_uses_execution_date(path, instrument_status, offset):
    effective = date(2026, 9, 9)
    execution_day = effective + timedelta(days=offset)

    class Repo:
        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({
                "symbol": ["00700.HK"], "lot_size": [100], "currency": ["HKD"],
                "lot_size_status": ["verified_snapshot"], "lot_size_as_of": [date(2026, 8, 1)],
                "instrument_status": [instrument_status], "instrument_status_as_of": [effective],
                "instrument_status_source": ["https://example.com/exchange-notice"],
                "lot_size_effective_from": [date(2026, 8, 1)],
                "lot_size_effective_to": [effective - timedelta(days=1)],
            })

    data = panel().head(3).with_columns(pl.Series("date", [execution_day - timedelta(days=1), execution_day, execution_day + timedelta(days=1)]))
    result = getattr(BacktestEngine(Repo()), f"simulate_{path}")(
        data, pl.Series([True, False, False]), pl.Series([False, True, False]),
        MatcherConfig(asset_type="hk", matching="open_t+1", lot_sizes={"00700.HK": 1}),
    )
    if offset < 0:
        assert result.trades
        assert result.trades[0].entry_date == execution_day.isoformat()
    else:
        assert not result.trades
        assert result.stats["execution"]["buy_instrument_inactive"] == 1


@pytest.mark.parametrize("path", PATHS)
def test_inactive_metadata_does_not_create_a_missing_historical_lot(path):
    class Repo:
        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({
                "symbol": ["00700.HK"], "lot_size": [None], "currency": ["HKD"],
                "lot_size_status": ["missing"], "instrument_status": ["delisted"],
                "instrument_status_as_of": [date(2026, 9, 9)],
                "instrument_status_source": ["https://example.com/exchange-notice"],
            })

    data = panel().head(3)
    result = getattr(BacktestEngine(Repo()), f"simulate_{path}")(
        data, pl.Series([True, False, False]), pl.Series([False, True, False]),
        MatcherConfig(asset_type="hk", matching="open_t+1", lot_sizes={"00700.HK": 1}),
    )
    assert not result.trades
    assert result.stats["execution"]["buy_lot_size_missing"] == 1
