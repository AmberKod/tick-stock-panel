from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, SimResult
from app.backtest.matrix import build_market_data_matrix, make_signal_matrix, rolling_mean
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.services import regime_builder
from app.strategy.engine import StrategyDef


def _strategy(**kwargs) -> StrategyDef:
    defaults = dict(
        meta={"id": "test", "name": "test", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 100.0},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=lambda df, params: pl.lit(True),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
        file_path=None,
    )
    defaults.update(kwargs)
    return StrategyDef(**defaults)


class _StrategyEngineStub:
    def __init__(self, strategy: StrategyDef) -> None:
        self.strategy = strategy

    def get(self, strategy_id: str) -> StrategyDef:
        return self.strategy

    def concept_config_fingerprint(self, strategy_id: str, params, overrides) -> str | None:
        # Stub: no concept dependency declared, mirrors the production default so
        # tests outside the concept-heat paths don't have to wire a real engine.
        return None


class _RepoStub:
    def __init__(self, data_dir=None) -> None:
        self.store = SimpleNamespace(data_dir=data_dir)

    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


class _EngineStub:
    def __init__(self, panel: pl.DataFrame, data_dir=None) -> None:
        self.panel = panel
        self.repo = _RepoStub(data_dir)
        self.load_args = None
        self.load_count = 0
        self.sim_panel: pl.DataFrame | None = None
        self.sim_matrix = None
        self.sim_entries: pl.Series | None = None

    def load_panel(self, symbols, start: date, end: date, columns=None, asset_type: str = "stock") -> pl.DataFrame:
        self.load_count += 1
        self.load_args = (symbols, start, end)
        self.load_asset_type = asset_type
        return self.panel

    def load_panel_for_backtest(self, symbols, start, end, feature_plan, asset_type="stock") -> pl.DataFrame:
        return self.load_panel(symbols, start, end, columns=sorted(feature_plan.base_columns), asset_type=asset_type)

    def load_market_data_matrix_for_backtest(
        self,
        symbols,
        start,
        end,
        feature_plan,
        asset_type="stock",
        **kwargs,
    ):
        panel = self.load_panel_for_backtest(
            symbols,
            start,
            end,
            feature_plan,
            asset_type=asset_type,
        )
        field_columns = (
            set(feature_plan.base_columns)
            | set(feature_plan.instrument_columns)
            | set(feature_plan.matrix_columns)
        )
        return build_market_data_matrix(panel, field_columns=field_columns)

    def simulate_portfolio(self, panel, entries, exits, config, progress_cb=None, cancel_event=None, entry_signal_ids=None, exit_signal_ids=None) -> SimResult:
        self.sim_panel = panel
        self.sim_entries = entries
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )

    def simulate_market_matrix(
        self,
        matrix,
        config,
        progress_cb=None,
        cancel_event=None,
        options=None,
    ) -> SimResult:
        self.sim_matrix = matrix
        return SimResult(
            equity_curve=[{"date": "2024-01-01", "value": config.initial_capital}],
            drawdown_curve=[{"date": "2024-01-01", "value": 0.0}],
            trades=[],
            per_symbol_stats=[],
            stats={"total_return": 0.0, "n_trades": 0},
        )


def test_basic_filter_only_limits_entries_not_panel_rows():
    start = date(2024, 1, 1)
    rows = []
    for i, amount in enumerate([1000.0, 0.0, 1000.0]):
        rows.append({
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=i),
            "open": 10.0 + i,
            "high": 10.0 + i,
            "low": 10.0 + i,
            "close": 10.0 + i,
            "volume": 100_000,
            "amount": amount,
            "signal_limit_up": False,
            "signal_limit_down": False,
        })
    panel = pl.DataFrame(rows).sort(["symbol", "date"])
    engine = _EngineStub(panel)
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(_strategy()))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start + timedelta(days=2),
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.shape == (3, 1)
    assert engine.sim_matrix.entry[:, 0].tolist() == [1, 0, 1]
    assert engine.load_args is not None
    assert engine.load_args[1] < start  # warmup 只用于计算, 不参与正式交易
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 2,
        "entry_trigger_filtered": 0,
        "entry_trigger_enabled": False,
    }


def test_non_matrix_strategy_applies_regime_filter_and_reports_config(tmp_path):
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=offset),
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 1000.0,
            "signal_limit_up": False,
            "signal_limit_down": False,
        }
        for offset in range(-1, 3)
    ]).sort(["symbol", "date"])
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [
            start - timedelta(days=1),
            start,
            start + timedelta(days=1),
            start + timedelta(days=2),
        ],
        "state": ["weak", "weak", "strong", "strong"],
        "score": [10, 10, 85, 85],
    }))
    engine = _EngineStub(panel, data_dir=tmp_path)
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(_strategy()))
    regime_filter = {"states": ["strong"]}

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start + timedelta(days=2),
        matching="close_t",
        mode="position",
        regime_filter=regime_filter,
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.entry[:, 0].tolist() == [0, 0, 1]
    assert result.config["regime_filter"] == regime_filter
    assert result.stats["selection"] == {
        "strategy_matches": 1,
        "entry_candidates": 1,
        "entry_trigger_filtered": 0,
        "entry_trigger_enabled": False,
    }


def test_regime_filter_matches_raw_five_level_states(tmp_path):
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=offset),
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 1000.0,
            "signal_limit_up": False,
            "signal_limit_down": False,
        }
        for offset in range(-1, 3)
    ]).sort(["symbol", "date"])
    regime_builder.upsert_regime_history(tmp_path, pl.DataFrame({
        "date": [
            start - timedelta(days=1),
            start,
            start + timedelta(days=1),
            start + timedelta(days=2),
        ],
        "state": ["weak", "lean_strong", "strong", "strong"],
        "score": [10, 60, 85, 85],
    }))

    def run_with(states: list[str]):
        engine = _EngineStub(panel, data_dir=tmp_path)
        service = StrategyBacktestService(
            engine=engine,
            strategy_engine=_StrategyEngineStub(_strategy()),
        )
        result = service.run(StrategyBacktestConfig(
            strategy_id="test",
            symbols=None,
            start=start,
            end=start + timedelta(days=2),
            matching="close_t",
            mode="position",
            regime_filter={"states": states},
        ))
        assert result.error is None
        assert engine.sim_matrix is not None
        return engine.sim_matrix.entry[:, 0].tolist()

    # 强势与偏强是两个独立档位; 只选强势时偏强日不入场
    assert run_with(["strong"]) == [0, 0, 1]
    assert run_with(["strong", "lean_strong"]) == [0, 1, 1]
    assert run_with(["lean_strong"]) == [0, 1, 0]


def test_selection_stats_explain_entry_trigger_filtering():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": symbol,
            "name": symbol,
            "date": start,
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 10.0,
            "volume": 1000.0,
            "amount": 1000.0,
            "signal_limit_up": symbol == "A",
            "signal_limit_down": False,
        }
        for symbol in ("A", "B")
    ]).sort(["symbol", "date"])
    engine = _EngineStub(panel)
    service = StrategyBacktestService(
        engine=engine,
        strategy_engine=_StrategyEngineStub(
            _strategy(entry_signals=["signal_limit_up"]),
        ),
    )

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start,
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 1,
        "entry_trigger_filtered": 1,
        "entry_trigger_enabled": True,
    }


def test_score_normalizes_inside_strategy_candidate_universe():
    panel = pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "date": [date(2024, 1, 1)] * 3,
        "factor": [10.0, 20.0, 1000.0],
    })
    universe = pl.Series([True, True, False], dtype=pl.Boolean)
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0}, "order_by": "score", "descending": True})

    scored = StrategyBacktestService._apply_score(panel, strategy, None, universe_mask=universe)
    scores = dict(zip(scored["symbol"].to_list(), scored["score"].to_list(), strict=True))

    assert scores["A"] == 0.0
    assert scores["B"] == 100.0
    assert scores["C"] == 0.0


@pytest.mark.parametrize("missing", ["column", "null", "nan", "infinite"])
def test_backtest_scoring_rejects_unavailable_candidate_factor(missing):
    panel = pl.DataFrame({
        "symbol": ["A", "B", "C"],
        "date": [date(2024, 1, 1)] * 3,
        "factor": [None, None, 100.0],
    })
    if missing == "column":
        panel = panel.drop("factor")
    elif missing != "null":
        value = float("nan") if missing == "nan" else float("inf")
        panel = panel.with_columns(
            pl.when(pl.col("symbol") != "C").then(value).otherwise(100.0).alias("factor"),
        )
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0}})

    with pytest.raises(ValueError, match="factor"):
        StrategyBacktestService._apply_score(
            panel, strategy, None, universe_mask=pl.Series([True, True, False]),
        )


def test_backtest_scoring_validates_each_candidate_date():
    panel = pl.DataFrame({
        "symbol": ["A", "A"],
        "date": [date(2024, 1, 1), date(2024, 1, 2)],
        "factor": [10.0, None],
    })
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0}})

    with pytest.raises(ValueError, match=r"2024-01-02.*factor"):
        StrategyBacktestService._apply_score(panel, strategy, None)


def test_backtest_scoring_rejects_candidates_without_a_complete_score():
    panel = pl.DataFrame({
        "symbol": ["A", "B"],
        "date": [date(2024, 1, 1)] * 2,
        "factor": [10.0, None],
        "other": [None, 20.0],
    })
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0, "other": 1.0}})

    with pytest.raises(ValueError, match=r"factor.*other"):
        StrategyBacktestService._apply_score(panel, strategy, None)


def test_backtest_scoring_keeps_partial_missing_scores_null_and_normalizes_by_date():
    panel = pl.DataFrame({
        "symbol": ["A", "B", "C", "A", "B", "C"],
        "date": [date(2024, 1, 1)] * 3 + [date(2024, 1, 2)] * 3,
        "factor": [10.0, 20.0, None, 200.0, 100.0, float("inf")],
    })
    strategy = SimpleNamespace(meta={"scoring": {"factor": 1.0}})
    scored = StrategyBacktestService._apply_score(panel, strategy, None)

    assert scored["symbol"].to_list() == panel["symbol"].to_list()
    assert scored["score"].to_list() == [0.0, 100.0, None, 100.0, 0.0, None]


def _strict_scoring_strategy(backend: str) -> StrategyDef:
    class NativeStrategy:
        def required_fields(self):
            return frozenset({"close", "signal_limit_down", "factor"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape,
                entry=(market.close > 0).astype(np.uint8),
                exit=market.limit_down_locked,
            )

    return _strategy(
        meta={"id": "test", "name": "test", "asset_types": ["us"],
              "scoring": {"factor": 1.0}, "params": []},
        basic_filter={"enabled": False},
        execution_backend=backend,
        required_features=frozenset({"close"}),
        filter_fn=(lambda df, params: pl.col("close") > 0) if backend == "polars_expr" else None,
        filter_history_fn=(lambda df, params: df.filter(pl.col("close") > 0)) if backend == "legacy" else None,
        exit_signals=["signal_limit_down"],
        matrix_strategy=NativeStrategy() if backend == "matrix_native" else None,
    )


def _strict_scoring_panel() -> pl.DataFrame:
    start = date(2024, 1, 2)
    return pl.DataFrame([
        {
            "symbol": symbol, "name": symbol,
            "date": start + timedelta(days=offset),
            "open": 10.0 + offset * 2, "high": 10.0 + offset * 2,
            "low": 10.0 + offset * 2, "close": 10.0 + offset * 2,
            "volume": 1000.0,
            "factor": 10.0 if (symbol, offset) in {("A.US", 0), ("B.US", 1)} else None,
            "signal_limit_up": False,
            "signal_limit_down": offset == (1 if symbol == "A.US" else 2),
        }
        for symbol in ("A.US", "B.US") for offset in range(-1, 4)
    ]).sort(["symbol", "date"])


@pytest.mark.parametrize("backend", ["polars_expr", "legacy", "matrix_native"])
def test_partial_scoring_excludes_entries_but_preserves_exit_prices_and_signals(backend):
    start = date(2024, 1, 2)
    panel = _strict_scoring_panel()
    engine = BacktestEngine(repo=None)
    engine.load_panel_for_backtest = lambda *args, **kwargs: panel
    engine.load_market_data_matrix_for_backtest = lambda *args, **kwargs: build_market_data_matrix(
        panel, field_columns={"factor", "signal_limit_down"},
    )
    service = StrategyBacktestService(engine, _StrategyEngineStub(_strict_scoring_strategy(backend)))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test", symbols=["A.US", "B.US"], asset_type="us",
        start=start, end=start + timedelta(days=1), mode="full",
        matching="open_t+1", fees_pct=0, slippage_bps=0,
    ))

    assert result.error is None
    assert len(result.trades) == 2
    trades = {trade["symbol"]: trade for trade in result.trades}
    assert trades["A.US"]["entry_date"] == "2024-01-03"
    assert trades["A.US"]["exit_date"] == "2024-01-04"
    assert trades["A.US"]["exit_price"] == 14.0
    assert trades["B.US"]["entry_date"] == "2024-01-04"
    assert trades["B.US"]["exit_date"] == "2024-01-05"
    assert trades["B.US"]["exit_price"] == 16.0
    assert all(trade["entry_score"] == 50.0 for trade in result.trades)
    assert result.stats["selection"]["entry_candidates"] == 2
    assert len(result.warnings) == 2
    assert all("1 个标的" in warning and "factor" in warning for warning in result.warnings)
    assert any("2024-01-02" in warning for warning in result.warnings)
    assert any("2024-01-03" in warning for warning in result.warnings)


@pytest.mark.parametrize("backend", ["polars_expr", "legacy", "matrix_native"])
def test_unavailable_scoring_returns_a_service_error_before_execution(backend):
    start = date(2024, 1, 2)
    panel = _strict_scoring_panel().drop("factor")
    engine = _EngineStub(panel)
    service = StrategyBacktestService(engine, _StrategyEngineStub(_strict_scoring_strategy(backend)))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test", symbols=None, asset_type="us",
        start=start, end=start + timedelta(days=1),
    ))

    assert result.error is not None
    assert "factor" in result.error and "不可计算" in result.error
    assert result.trades == []
    assert engine.sim_matrix is None


@pytest.mark.parametrize("backend", ["polars_expr", "legacy", "matrix_native"])
def test_backtest_rejects_an_unscorable_date_even_when_other_dates_are_valid(backend):
    panel = _strict_scoring_panel().with_columns(
        pl.when(pl.col("date") == date(2024, 1, 3)).then(None).otherwise(pl.col("factor")).alias("factor"),
    )
    engine = _EngineStub(panel)
    engine.load_market_data_matrix_for_backtest = lambda *args, **kwargs: build_market_data_matrix(
        panel, field_columns={"factor"},
    )
    service = StrategyBacktestService(engine, _StrategyEngineStub(_strict_scoring_strategy(backend)))
    result = service.run(StrategyBacktestConfig(
        strategy_id="test", symbols=None, asset_type="us",
        start=date(2024, 1, 2), end=date(2024, 1, 3),
    ))
    assert result.error is not None
    assert "2024-01-03" in result.error and "factor" in result.error
    assert engine.sim_matrix is None


@pytest.mark.parametrize("missing_column", [True, False])
def test_unavailable_basic_filter_returns_a_service_error(missing_column):
    panel = _strict_scoring_panel()
    if not missing_column:
        panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount"))
    strategy = _strict_scoring_strategy("polars_expr")
    strategy.basic_filter = {"amount_min": 1}
    engine = _EngineStub(panel)
    service = StrategyBacktestService(engine, _StrategyEngineStub(strategy))
    result = service.run(StrategyBacktestConfig(
        strategy_id="test", symbols=None, asset_type="us",
        start=date(2024, 1, 2), end=date(2024, 1, 3),
    ))
    assert result.error is not None
    assert "amount" in result.error and "不可计算" in result.error
    assert engine.sim_matrix is None


@pytest.mark.parametrize("backend", ["polars_expr", "legacy", "matrix_native"])
@pytest.mark.parametrize("whole_date_missing", [False, True])
def test_backtest_basic_filter_reports_partial_and_daily_input_gaps(backend, whole_date_missing):
    panel = _strict_scoring_panel().with_columns(
        pl.when(
            (pl.col("symbol") == "A.US")
            & ((pl.col("date") != date(2024, 1, 3)) | pl.lit(not whole_date_missing))
        ).then(1000.0).otherwise(None).alias("amount"),
    )
    strategy = _strict_scoring_strategy(backend)
    strategy.meta["scoring"] = {}
    strategy.basic_filter = {"amount_min": 1}
    engine = _EngineStub(panel)
    result = StrategyBacktestService(engine, _StrategyEngineStub(strategy)).run(StrategyBacktestConfig(
        strategy_id="test", symbols=None, asset_type="us",
        start=date(2024, 1, 2), end=date(2024, 1, 3),
    ))
    if whole_date_missing:
        assert result.error is not None and "2024-01-03" in result.error and "amount" in result.error
        assert engine.sim_matrix is None
    else:
        assert result.error is None
        assert len(result.warnings) == 2
        assert all("amount" in warning and "1 个标的" in warning for warning in result.warnings)


def test_backtest_price_assumptions_do_not_claim_unverified_adjustments():
    config = StrategyBacktestConfig(
        strategy_id="test", symbols=None, asset_type="us",
        start=date(2024, 1, 2), end=date(2024, 1, 3),
    )
    assumptions = StrategyBacktestService._config_to_dict(config)["execution_assumptions"]

    assert assumptions["price_basis"] == "stored_daily_ohlc"
    assert assumptions["corporate_actions_simulated"] is False
    assert "复权" in assumptions["price_basis_note"]


def test_full_mode_executes_every_candidate_with_strategy_rules():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1, "amount": 1000.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1, "amount": 0.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=2), "open": 20.0, "high": 20.0, "low": 20.0, "close": 20.0, "volume": 1, "amount": 1000.0, "signal_limit_up": False, "signal_limit_down": False},
    ]).sort(["symbol", "date"])

    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    engine.load_panel_for_backtest = lambda symbols, s, e, plan, asset_type="stock": panel  # type: ignore[method-assign]
    strategy = _strategy(
        filter_fn=lambda df, params: pl.col("date") == start,
        max_hold_days=1,
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    result = service.run(StrategyBacktestConfig(
        strategy_id="test",
        symbols=None,
        start=start,
        end=start,
        mode="full",
        matching="open_t+1",
        fees_pct=0,
        slippage_bps=0,
        holding_days=1,
    ))

    assert result.error is None
    assert result.stats["full_kind"] == "candidate_execution"
    assert result.stats["n_candidates"] == 1
    assert result.stats["n_trades"] == 1
    assert result.trades[0]["entry_date"] == str(start + timedelta(days=1))
    assert result.trades[0]["exit_reason"] == "max_hold"
    assert result.stats["avg_return"] == round(20 / 11 - 1, 4)


def test_matrix_native_strategy_uses_shared_orchestrator_path():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={"id": "native", "name": "native", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": True, "amount_min": 100.0},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        required_features=frozenset({"amount"}),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    result = service.run(StrategyBacktestConfig(
        strategy_id="native",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        mode="position",
    ))

    assert result.error is None
    assert engine.sim_matrix is not None
    assert engine.sim_matrix.entry[:, 0].tolist() == [1, 1]
    assert result.stats["execution_backend"] == "matrix_native"
    assert result.stats["selection"] == {
        "strategy_matches": 2,
        "entry_candidates": 2,
        "entry_trigger_filtered": 0,
        "entry_trigger_enabled": False,
    }


def test_matrix_native_accepts_legacy_default_signal_overrides_but_rejects_replacements():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return 1

        def compute_signals(self, market, params):
            return make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8))

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={"id": "native_defaults", "name": "native", "scoring": {}, "params": [], "limit": 100},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        entry_signals=["signal_custom_entry"],
        exit_signals=["signal_custom_exit"],
        required_features=frozenset(),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))

    accepted = service.run(StrategyBacktestConfig(
        strategy_id="native_defaults",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        overrides={
            "entry_signals": ["signal_custom_entry"],
            "exit_signals": ["signal_custom_exit"],
        },
    ))
    assert accepted.error is None

    rejected = service.run(StrategyBacktestConfig(
        strategy_id="native_defaults",
        symbols=None,
        start=start,
        end=start + timedelta(days=1),
        matching="close_t",
        overrides={"entry_signals": ["signal_other"]},
    ))
    assert rejected.error == "matrix_native 策略的进出场信号由策略协议生成，不支持列信号覆盖"


def test_matrix_optimizer_preparation_loads_and_builds_base_data_once():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {"symbol": "A", "name": "A", "date": start, "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1.0, "amount": 1000.0, "raw_close": 10.0, "raw_high": 10.0, "signal_limit_up": False, "signal_limit_down": False},
        {"symbol": "A", "name": "A", "date": start + timedelta(days=1), "open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0, "volume": 1.0, "amount": 1000.0, "raw_close": 11.0, "raw_high": 11.0, "signal_limit_up": False, "signal_limit_down": False},
    ])

    class NativeStrategy:
        def required_fields(self):
            return frozenset({"open", "high", "low", "close", "volume"})

        def required_warmup_bars(self, params):
            return int(params.get("warmup", 1))

        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape,
                entry=np.ones(market.shape, dtype=np.uint8),
            )

    engine = _EngineStub(panel)
    strategy = _strategy(
        meta={
            "id": "native",
            "name": "native",
            "scoring": {},
            "params": [{"id": "warmup", "type": "int", "default": 1, "min": 1, "max": 10}],
            "limit": 100,
        },
        basic_filter={"enabled": False},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=NativeStrategy(),
        required_features=frozenset({"amount"}),
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))
    configs = [
        StrategyBacktestConfig(
            strategy_id="native",
            symbols=None,
            start=start,
            end=start + timedelta(days=1),
            params={"warmup": warmup},
            matching="close_t",
        )
        for warmup in (1, 10)
    ]

    prepared = service.prepare_matrix_optimization(configs)
    results = [service.run(config, prepared=prepared) for config in configs]

    assert engine.load_count == 1
    assert prepared.market_data.nbytes > 0
    assert all(result.error is None for result in results)
    assert all(result.stats["shared_market_data"] is True for result in results)
    assert all(result.stats["shared_market_data_bytes"] == prepared.market_data.nbytes for result in results)


def test_matrix_prepare_signature_includes_regime_filter():
    base = dict(
        strategy_id="native",
        symbols=None,
        start=date(2024, 1, 1),
        end=date(2024, 1, 2),
    )
    without_filter = StrategyBacktestConfig(**base)
    with_filter = StrategyBacktestConfig(**base, regime_filter={"states": ["strong"]})

    assert StrategyBacktestService._matrix_prepare_signature(without_filter) != (
        StrategyBacktestService._matrix_prepare_signature(with_filter)
    )


def test_matrix_cache_preserves_trades_daily_equity_and_core_stats():
    start = date(2024, 1, 1)
    panel = pl.DataFrame([
        {
            "symbol": "A",
            "name": "A",
            "date": start + timedelta(days=offset),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000.0,
            "raw_close": close,
            "raw_high": close,
            "signal_limit_up": False,
            "signal_limit_down": False,
        }
        for offset, close in enumerate((10.0, 11.0, 12.0, 11.0, 13.0, 12.0))
    ])

    class RollingEntry:
        def required_fields(self):
            return frozenset({"close"})

        def required_warmup_bars(self, params):
            return 2

        def compute_signals(self, market, params):
            entry = market.close >= rolling_mean(market.close, 2)
            return make_signal_matrix(market.shape, entry=entry.astype(np.uint8))

    engine = BacktestEngine(repo=None)  # type: ignore[arg-type]
    engine.load_market_data_matrix_for_backtest = (  # type: ignore[method-assign]
        lambda symbols, s, e, plan, asset_type="stock", **kwargs: build_market_data_matrix(
            panel,
            field_columns=(
                set(plan.base_columns)
                | set(plan.instrument_columns)
                | set(plan.matrix_columns)
            ),
        )
    )
    strategy = _strategy(
        meta={"id": "rolling", "name": "rolling", "scoring": {}, "params": [], "limit": 100},
        basic_filter={"enabled": False},
        filter_fn=None,
        execution_backend="matrix_native",
        matrix_strategy=RollingEntry(),
        required_features=frozenset({"amount"}),
        max_hold_days=1,
    )
    service = StrategyBacktestService(engine=engine, strategy_engine=_StrategyEngineStub(strategy))
    config = StrategyBacktestConfig(
        strategy_id="rolling",
        symbols=None,
        start=start,
        end=start + timedelta(days=5),
        matching="close_t",
        fees_pct=0,
        slippage_bps=0,
        max_positions=1,
    )

    uncached = service.run(config)
    prepared = service.prepare_matrix_optimization([config])
    cached = service.run(config, prepared=prepared)
    cached_again = service.run(config, prepared=prepared)
    prepared.compute_cache.close()

    assert uncached.error is None
    assert cached.error is None
    assert cached_again.error is None
    assert cached.trades == uncached.trades
    assert cached.equity_curve == uncached.equity_curve
    assert cached.drawdown_curve == uncached.drawdown_curve
    for name in (
        "total_return",
        "annual_return",
        "max_drawdown",
        "sharpe",
        "sortino",
        "n_trades",
    ):
        assert cached.stats[name] == uncached.stats[name]
        assert cached_again.stats[name] == uncached.stats[name]
    assert cached_again.stats["matrix_compute_cache"]["hits"] > 0
