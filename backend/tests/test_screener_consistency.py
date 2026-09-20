"""Regression cases for the shared daily screening contract."""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    build_basic_filter_mask,
    build_market_data_matrix,
    make_signal_matrix,
)
from app.strategy import engine as engine_module
from app.strategy.engine import (
    DEFAULT_BASIC_FILTER,
    StrategyDataContext,
    StrategyDef,
    StrategyEngine,
    market_basic_filter,
)
from app.strategy.industry_heat import attach_industry_heat
from app.strategy.portfolio_constraints import industry_map_for_market


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["A.US", "B.US", "C.US", "D.US"],
        "date": [date(2026, 9, 8)] * 4,
        "open": [10.0] * 4,
        "high": [11.0] * 4,
        "low": [9.0] * 4,
        "close": [10.0] * 4,
        "volume": [1000.0] * 4,
        "change_pct": [-0.03, 0.02, 0.04, 0.09],
        "pe_ttm": [10.0, 20.0, 40.0, None],
        "pb": [0.5, 2.0, 4.0, 8.0],
    })


@pytest.mark.parametrize("config,expected", [
    ({"change_pct_min": 0.0, "change_pct_max": 0.05}, ["B.US", "C.US"]),
    ({"pe_ttm_min": 15.0, "pe_ttm_max": 30.0}, ["B.US"]),
    ({"pb_min": 1.0, "pb_max": 5.0}, ["B.US", "C.US"]),
])
def test_polars_and_matrix_apply_the_same_bounds(config, expected):
    panel = _panel()
    market = build_market_data_matrix(panel, field_columns={"change_pct", "pe_ttm", "pb"})
    rows = StrategyEngine._apply_basic_filter(panel, config)
    mask = build_basic_filter_mask(market, config)
    assert sorted(rows["symbol"].to_list()) == expected
    assert [symbol for symbol, selected in zip(market.symbols, mask[0], strict=True) if selected] == expected


@pytest.mark.parametrize("field,prefix", [
    ("change_pct", "change_pct"), ("pe_ttm", "pe_ttm"), ("pb", "pb"),
    ("amount", "amount"), ("turnover_rate", "turnover"),
    ("total_shares", "market_cap"), ("float_shares", "float_cap"),
])
@pytest.mark.parametrize("missing", [True, False])
def test_enabled_filter_cannot_silently_ignore_unavailable_data(field, prefix, missing):
    panel = _panel().drop([name for name in (field,) if name in _panel().columns])
    if not missing:
        panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias(field))
    config = {f"{prefix}_min": 0}
    with pytest.raises(ValueError, match=field):
        StrategyEngine._apply_basic_filter(panel, config)
    market = build_market_data_matrix(panel, field_columns={field} if not missing else set())
    with pytest.raises(ValueError, match=field):
        build_basic_filter_mask(market, config)


def test_basic_filter_dependencies_include_enabled_numeric_fields():
    assert engine_module.basic_filter_dependencies({"enabled": False, "pb_min": 1}) == set()
    config = {f"{prefix}_min": 0 for prefix in (
        "change_pct", "pe_ttm", "pb", "market_cap", "float_cap", "amount", "turnover",
    )}
    assert engine_module.basic_filter_dependencies(config) == {
        "change_pct", "pe_ttm", "pb", "close", "total_shares", "float_shares", "amount", "turnover_rate",
    }


def test_market_defaults_preserve_strategy_and_user_explicit_values():
    base = {**DEFAULT_BASIC_FILTER, "market_cap_min": 5e8}
    assert market_basic_filter(base, "hk")["market_cap_min"] is None
    retained = market_basic_filter(base, "hk", explicit_keys=frozenset({"market_cap_min"}))
    assert retained["market_cap_min"] == 5e8
    overridden = market_basic_filter(base, "us", overrides={"market_cap_min": 9e8})
    assert overridden["market_cap_min"] == 9e8
    assert overridden["exclude_st"] is False
    assert overridden["boards"] == []
    assert base["market_cap_min"] == 5e8


def test_loader_retains_basic_filter_provenance(tmp_path: Path):
    strategy = tmp_path / "explicit.py"
    strategy.write_text(
        "import polars as pl\n"
        "META = {'id': 'explicit', 'asset_types': ['hk'], 'basic_filter': {'pb_max': 3}}\n"
        "BASIC_FILTER = {'market_cap_min': 123}\n"
        "def filter(df, params):\n    return pl.lit(True)\n",
        encoding="utf-8",
    )
    loaded = StrategyEngine([tmp_path]).get("explicit")
    assert loaded.basic_filter_explicit_keys == frozenset({"pb_max", "market_cap_min"})


def _write_industries(data_dir: Path, sectors: list[str | None]) -> None:
    directory = data_dir / "instruments"
    directory.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["A.US", "B.US", "C.US", "D.US"], "sector": sectors}).write_parquet(
        directory / "us_instruments.parquet"
    )


def test_heat_preserves_unknown_members_and_uses_each_date(tmp_path):
    _write_industries(tmp_path, ["Tech", "Tech", "Tech", None])
    current = _panel().with_columns(pl.Series("change_pct", [0.01, 0.02, 0.03, 0.9]))
    earlier = current.with_columns(
        pl.lit(date(2026, 9, 7)).alias("date"),
        (pl.col("change_pct") * -1).alias("change_pct"),
    )
    panel = pl.concat([current, earlier])
    result = attach_industry_heat(panel, tmp_path, market="us")
    assert result.height == panel.height
    assert result.filter(pl.col("symbol") == "D.US")["industry_heat"].null_count() == 2
    np.testing.assert_allclose(
        result.filter(pl.col("symbol") == "A.US").sort("date")["industry_heat"].to_numpy(),
        [-0.02, 0.02],
    )


def test_heat_requires_three_finite_members(tmp_path):
    _write_industries(tmp_path, ["Tech", "Tech", "Tech", None])
    panel = _panel().with_columns(pl.Series("change_pct", [0.01, 0.02, float("nan"), 0.9]))
    result = attach_industry_heat(panel, tmp_path, market="us")
    assert result["industry_heat"].null_count() == 4


def test_industry_cache_is_scoped_to_directory_and_refreshes_with_mapping(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    _write_industries(first, ["Tech"] * 4)
    _write_industries(second, ["Banks"] * 4)
    assert industry_map_for_market(first, "us")["A.US"] == "Tech"
    assert industry_map_for_market(second, "us")["A.US"] == "Banks"
    _write_industries(first, ["Healthcare"] * 4)
    assert industry_map_for_market(first, "us")["A.US"] == "Healthcare"


class _PriceStrategy:
    def required_fields(self):
        return frozenset({"close"})

    def required_warmup_bars(self, params):
        return 0

    def compute_signals(self, market, params):
        return make_signal_matrix(market.shape, entry=(market.close >= params.get("price", 0)).astype(np.uint8))


def _engine(tmp_path: Path, *, scoring: dict, order_by="score", descending=True) -> StrategyEngine:
    engine = StrategyEngine([], data_dir=tmp_path)
    for backend in ("polars_expr", "matrix_native"):
        engine._strategies[backend] = StrategyDef(
            meta={"id": backend, "asset_types": ["us"], "timeframes": ["1d"], "scoring": scoring,
                  "order_by": order_by, "descending": descending},
            basic_filter={"enabled": False},
            entry_signals=[], exit_signals=[], stop_loss=None, trailing_stop=None,
            trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
            max_hold_days=None, filter_fn=(lambda df, params: pl.col("close") >= params.get("price", 0)) if backend == "polars_expr" else None,
            filter_history_fn=None, lookback_days=1, source="custom", execution_backend=backend,
            matrix_strategy=_PriceStrategy() if backend == "matrix_native" else None,
        )
    return engine


def _context(panel: pl.DataFrame, *, historical=False, shared=False) -> StrategyDataContext:
    return StrategyDataContext(
        asset_type="us", timeframe="1d", as_of=panel["date"].max(), current=panel, history=panel,
        is_historical=historical,
        market=build_market_data_matrix(panel) if shared else None,
    )


@pytest.mark.parametrize("shared", [False, True])
def test_heat_is_computed_before_pool_and_strategy_filters(tmp_path, shared):
    _write_industries(tmp_path, ["Tech", "Tech", "Tech", None])
    panel = _panel().with_columns(
        pl.Series("close", [10.0, 5.0, 6.0, 8.0]),
        pl.Series("change_pct", [0.01, 0.02, 0.03, 0.9]),
    )
    engine = _engine(tmp_path, scoring={"industry_heat": 1})
    results = [engine.run(backend, _context(panel, shared=shared), params={"price": 10}, pool=["A.US"])
               for backend in ("polars_expr", "matrix_native")]
    for result in results:
        assert [row["symbol"] for row in result.rows] == ["A.US"]
        assert result.rows[0]["industry_heat"] == pytest.approx(0.02)
        assert result.rows[0]["score"] == pytest.approx(50)
        assert result.industry_mapping_version


def test_unknown_heat_members_are_explained_in_both_candidate_paths(tmp_path):
    _write_industries(tmp_path, ["Tech", "Tech", "Tech", None])
    engine = _engine(tmp_path, scoring={"industry_heat": 1})
    for backend in ("polars_expr", "matrix_native"):
        result = engine.run(backend, _context(_panel()))
        assert [row["symbol"] for row in result.rows] == ["A.US", "B.US", "C.US"]
        assert any("1" in warning and "industry_heat" in warning for warning in result.warnings)


@pytest.mark.parametrize("backend", ["polars_expr", "matrix_native"])
@pytest.mark.parametrize("config", ["pe_ttm", "pb", "industry_heat", "portfolio"])
def test_historical_screening_rejects_untraceable_snapshots(tmp_path, backend, config):
    engine = _engine(tmp_path, scoring={config: 1} if config != "portfolio" else {})
    overrides = {"portfolio": {"enabled": True}} if config == "portfolio" else {}
    with pytest.raises(ValueError, match="可追溯"):
        engine.run(backend, _context(_panel(), historical=True), overrides=overrides)


@pytest.mark.parametrize("descending", [True, False])
def test_matrix_and_polars_sort_order_by_in_the_same_direction(tmp_path, descending):
    engine = _engine(tmp_path, scoring={}, order_by="close", descending=descending)
    panel = _panel().with_columns(pl.Series("close", [9.0, 12.0, 11.0, 10.0]))
    candidates = [
        [row["symbol"] for row in engine.run(backend, _context(panel)).rows]
        for backend in ("polars_expr", "matrix_native")
    ]
    assert candidates[0] == candidates[1]


@pytest.mark.parametrize("backend", ["polars_expr", "matrix_native"])
def test_missing_order_by_values_are_excluded_with_a_warning(tmp_path, backend):
    engine = _engine(tmp_path, scoring={}, order_by="volume")
    panel = _panel().with_columns(pl.Series("volume", [100.0, None, 300.0, float("inf")]))
    result = engine.run(backend, _context(panel))
    assert [row["symbol"] for row in result.rows] == ["C.US", "A.US"]
    assert any("volume" in warning for warning in result.warnings)


@pytest.mark.parametrize("backend", ["polars_expr", "matrix_native"])
def test_no_valid_order_by_data_is_an_error(tmp_path, backend):
    engine = _engine(tmp_path, scoring={}, order_by="volume")
    panel = _panel().with_columns(pl.lit(None, dtype=pl.Float64).alias("volume"))
    with pytest.raises(ValueError, match="volume"):
        engine.run(backend, _context(panel))


def test_matrix_screening_only_validates_scoring_on_requested_date(tmp_path):
    engine = _engine(tmp_path, scoring={"volume": 1})
    current = _panel()
    history = pl.concat([
        current.with_columns(
            pl.lit(date(2026, 9, 7)).alias("date"),
            pl.lit(None, dtype=pl.Float64).alias("volume"),
        ),
        current,
    ])
    context = StrategyDataContext(
        asset_type="us", timeframe="1d", as_of=date(2026, 9, 8), current=current, history=history,
    )
    result = engine.run("matrix_native", context)
    assert result.total == 4
    assert not result.warnings


@pytest.mark.parametrize("market", ["hk", "us"])
@pytest.mark.parametrize("strategy_id", ["ma_golden_cross", "macd_golden", "boll_breakout", "trend_breakout"])
def test_multi_market_builtins_resolve_their_defaults_without_disabling_all_filters(market, strategy_id):
    builtin = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    engine = StrategyEngine([builtin])
    strategy = engine.get(strategy_id)
    basic = market_basic_filter(strategy.basic_filter, market, explicit_keys=strategy.basic_filter_explicit_keys)
    assert basic.get("enabled", True)
    assert basic["market_cap_min"] is None
    assert basic["amount_min"] > 0
    symbol = "00700.HK" if market == "hk" else "AAPL.US"
    start = date(2026, 6, 1)
    panel = pl.DataFrame([
        {"symbol": symbol, "date": start + timedelta(days=i), "open": 10 + i * 0.1,
         "high": 10.5 + i * 0.1, "low": 9.5 + i * 0.1, "close": 10 + i * 0.1,
         "volume": 1e7 if i == 70 else 1e6, "amount": 2e8, "change_pct": 0.01}
        for i in range(71)
    ])
    as_of = panel["date"].max()
    context = StrategyDataContext(
        asset_type=market, timeframe="1d", as_of=as_of,
        current=panel.filter(pl.col("date") == as_of), history=panel,
    )
    result = engine.run(strategy_id, context)
    assert result.as_of == as_of


def test_trend_market_defaults_never_erase_user_cap_condition():
    builtin = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
    strategy = StrategyEngine([builtin]).get("trend_breakout")
    resolved = market_basic_filter(
        strategy.basic_filter, "hk", explicit_keys=strategy.basic_filter_explicit_keys,
        overrides={"market_cap_min": 7e8},
    )
    assert resolved["market_cap_min"] == 7e8
