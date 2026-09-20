"""Concept snapshots use synthetic inputs and a fixed market clock, never live data."""
import json
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.strategy.concept_heat import (
    ConceptMappingSnapshot,
    attach_concept_heat,
    concept_heat_availability,
    concept_heat_required,
    load_concept_mapping,
)

DAY = date(2026, 9, 10)
SYMBOLS = ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"]


def write_mapping(data_dir, *, concepts=None):
    directory = data_dir / "ext_data" / "ext_gn_ths"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(json.dumps({
        "id": "ext_gn_ths", "label": "概念", "mode": "snapshot", "fields": [],
        "updated_at": "2026-09-10T09:00:00+08:00",
    }), encoding="utf-8")
    pl.DataFrame({
        "symbol": SYMBOLS,
        "所属概念": concepts or [" 热点 ; 冷点;热点; ", "热点", "热点", "冷点", "冷点"],
    }).write_parquet(directory / "part.parquet")
    return directory


def quote_frame():
    return pl.DataFrame({
        "symbol": SYMBOLS, "date": [DAY] * 5,
        "change_pct": [0.03, 0.05, 0.01, -0.01, 0.01], "close": [10.0] * 5,
    })


def test_golden_mean_keeps_rows_and_deduplicates_members(tmp_path):
    write_mapping(tmp_path)
    snapshot = load_concept_mapping(tmp_path, "cn")
    assert snapshot.status == "available"
    assert snapshot.pairs.height == 6
    quotes = pl.concat([
        quote_frame().head(1).with_columns(pl.lit(0.99).alias("change_pct")),
        quote_frame(),
        quote_frame().head(1).with_columns(pl.lit(float("nan")).alias("change_pct")),
    ])
    result = attach_concept_heat(quotes, snapshot, market="cn")
    assert result["symbol"].to_list() == quotes["symbol"].to_list()
    assert result["concept_heat"].to_list() == pytest.approx([0.02, 0.02, 0.03, 0.03, 0.01, 0.01, 0.02])


def test_invalid_values_and_small_concepts_are_null(tmp_path):
    write_mapping(tmp_path)
    snapshot = load_concept_mapping(tmp_path, "cn")
    quotes = quote_frame().with_columns(
        pl.Series("change_pct", ["0.03", "inf", None, "-0.01", "0.01"]),
        pl.lit(99.0).alias("concept_heat"),
    )
    result = attach_concept_heat(quotes, snapshot, market="cn")
    assert result["concept_heat"].to_list() == pytest.approx([0.01, None, None, 0.01, 0.01], nan_ok=True)
    missing = attach_concept_heat(quotes.drop("change_pct"), snapshot, market="cn")
    assert missing["concept_heat"].null_count() == 5


def test_empty_frame_does_not_fabricate_a_row(tmp_path):
    write_mapping(tmp_path)
    snapshot = load_concept_mapping(tmp_path)
    assert attach_concept_heat(pl.DataFrame(), snapshot).height == 0
    assert attach_concept_heat(quote_frame().head(0), snapshot).height == 0


def test_formula_isolates_dates_and_market(tmp_path):
    write_mapping(tmp_path)
    snapshot = load_concept_mapping(tmp_path, "cn")
    prior = quote_frame().with_columns(
        pl.lit(date(2026, 9, 9)).alias("date"), pl.col("change_pct") * -1,
    )
    result = attach_concept_heat(pl.concat([prior, quote_frame()]), snapshot, market="cn")
    assert result["concept_heat"].to_list() == pytest.approx([-.02, -.03, -.03, -.01, -.01, .02, .03, .03, .01, .01])
    assert attach_concept_heat(quote_frame(), snapshot, market="hk")["concept_heat"].null_count() == 5


def test_mapping_cache_isolates_directory_market_and_versions(tmp_path):
    first = tmp_path / "first"
    assert load_concept_mapping(first, "cn").status != "available"
    directory = write_mapping(first)
    snapshot = load_concept_mapping(first, "CN")
    assert load_concept_mapping(first, "cn") is snapshot
    assert load_concept_mapping(first, "hk").status == "missing_mapping"
    second = tmp_path / "second"
    write_mapping(second, concepts=["另一个"] * 5)
    assert load_concept_mapping(second, "cn").version != snapshot.version
    pl.DataFrame({"symbol": SYMBOLS, "所属概念": ["更新概念"] * 5}).write_parquet(directory / "part.parquet")
    updated = load_concept_mapping(first, "cn")
    assert updated.version != snapshot.version
    assert updated.pairs["concept"].unique().to_list() == ["更新概念"]


def test_mapping_rejects_bare_codes_and_bad_schema(tmp_path):
    directory = write_mapping(tmp_path)
    pl.DataFrame({"symbol": ["600001", "600002.HK", " 600003.sh ", "ABC.US"], "所属概念": ["题材"] * 4}).write_parquet(directory / "part.parquet")
    snapshot = load_concept_mapping(tmp_path, "cn")
    assert snapshot.pairs["symbol"].to_list() == ["600003.SH"]
    pl.DataFrame({"symbol": SYMBOLS}).write_parquet(directory / "part.parquet")
    assert load_concept_mapping(tmp_path, "cn").status == "invalid_source"
    (directory / "part.parquet").write_bytes(b"not parquet")
    assert load_concept_mapping(tmp_path, "cn").status == "read_error"


def test_missing_updated_at_is_not_fabricated(tmp_path):
    directory = write_mapping(tmp_path)
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    config.pop("updated_at")
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
    assert load_concept_mapping(tmp_path, "cn").updated_at is None


@pytest.mark.parametrize("as_of,quote_date,historical,code", [
    (DAY, date(2026, 9, 4), False, "stale_quote"),
    (DAY, date(2026, 9, 11), False, "future_quote"),
    (DAY, None, False, "missing_quote_date"),
    (date(2026, 9, 9), DAY, False, "noncurrent_target"),
    (DAY, DAY, True, "historical_membership"),
])
def test_availability_rejects_unproven_time(as_of, quote_date, historical, code):
    snapshot = ConceptMappingSnapshot(pl.DataFrame(), "available", "", "version", None)
    metadata = concept_heat_availability(snapshot, market="cn", as_of=as_of,
        quote_date=quote_date, current_market_date=DAY, historical=historical)
    assert metadata["status"] == "unavailable"
    assert metadata["reason_code"] == code


def test_zero_weight_without_other_reference_does_not_require_mapping():
    assert not concept_heat_required({"concept_heat": 0}, set())
    assert concept_heat_required({"concept_heat": 0}, {"concept_heat"})
    assert concept_heat_required({"concept_heat": 0.1}, set())


def test_complete_international_symbols_follow_provider_grammar(tmp_path):
    directory = write_mapping(tmp_path)
    pl.DataFrame({"symbol": [" BRK.A.us ", "BRK-B.US", "ABC1.US", "BRK.A", "700.HK"],
                  "所属概念": ["金融"] * 5}).write_parquet(directory / "part.parquet")
    us = load_concept_mapping(tmp_path, "us")
    assert set(us.pairs["symbol"]) == {"BRK.A.US", "BRK-B.US", "ABC1.US"}
    quotes = pl.DataFrame({"symbol": ["BRK.A.US", "BRK-B.US", "ABC1.US"], "change_pct": [.01, .02, .03]})
    assert attach_concept_heat(quotes, us, market="us")["concept_heat"].to_list() == pytest.approx([.02] * 3)
    assert load_concept_mapping(tmp_path, "hk").pairs["symbol"].to_list() == ["00700.HK"]


class AllEntries:
    def required_fields(self):
        return frozenset({"close"})

    def required_warmup_bars(self, params):
        return 1

    def compute_signals(self, market, params):
        from app.backtest.matrix import make_signal_matrix

        return make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8))


def concept_engine(tmp_path, monkeypatch):
    from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine

    monkeypatch.setattr("app.markets.get_profile", lambda market: SimpleNamespace(today=lambda: DAY))
    write_mapping(tmp_path)
    engine = StrategyEngine(data_dir=tmp_path)
    strategy = StrategyDef(
        meta={"id": "ordinary", "name": "概念样本", "scoring": {"concept_heat": 1.0},
              "order_by": "score", "asset_types": ["stock", "hk", "us", "etf"]},
        basic_filter={"enabled": False}, entry_signals=[], exit_signals=[],
        stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=lambda frame, params: pl.lit(True), filter_history_fn=None,
        lookback_days=1, source="custom",
    )
    engine._strategies["ordinary"] = strategy
    engine._strategies["matrix"] = replace(strategy, meta={**strategy.meta, "id": "matrix"},
                                             filter_fn=None, execution_backend="matrix_native", matrix_strategy=AllEntries())
    current = quote_frame().with_columns(pl.lit(10.0).alias("open"), pl.lit(10.0).alias("high"),
                                        pl.lit(10.0).alias("low"), pl.lit(1000.0).alias("volume"))
    history = pl.concat([current.with_columns(pl.lit(date(2026, 9, 9)).alias("date")), current])
    return engine, StrategyDataContext(asset_type="stock", timeframe="1d", as_of=DAY, current=current, history=history)


def test_engine_normal_matrix_shared_and_composite_use_same_full_slice(tmp_path, monkeypatch):
    from app.backtest.matrix import build_market_data_matrix
    from app.strategy.engine import CompositeChild, CompositeSpec

    engine, context = concept_engine(tmp_path, monkeypatch)
    ordinary = engine.run("ordinary", context)
    native = engine.run("matrix", context)
    shared_market = build_market_data_matrix(context.history, field_columns={"change_pct", "concept_heat"})
    shared = engine.run("matrix", replace(context, market=shared_market))
    assert ordinary.scores == pytest.approx(native.scores)
    assert ordinary.scores == pytest.approx(shared.scores)
    assert [row["symbol"] for row in ordinary.rows] == [SYMBOLS[1], SYMBOLS[2], SYMBOLS[0], SYMBOLS[3], SYMBOLS[4]]
    by_symbol = {row["symbol"]: row["concept_heat"] for row in ordinary.rows}
    assert by_symbol[SYMBOLS[0]] == pytest.approx(.02)
    assert engine.run("ordinary", context, pool=[SYMBOLS[0]]).rows[0]["concept_heat"] == pytest.approx(.02)
    prepared = engine._with_concept_heat(replace(context, market=shared_market), load_concept_mapping(tmp_path))
    assert prepared.history.filter(pl.col("date") < DAY)["concept_heat"].null_count() == 5
    assert np.isnan(prepared.market.fields["concept_heat"][0]).all()
    assert np.isfinite(prepared.market.fields["concept_heat"][-1]).all()
    assert prepared.market is not shared_market
    assert "concept_heat" not in shared_market.fields
    batch = engine.run_all(context, strategy_ids=["ordinary", "matrix"])
    assert batch["ordinary"].scores == pytest.approx(batch["matrix"].scores)
    assert ordinary.concept_heat_metadata["valid_concepts"] == 2
    engine._strategies["blend"] = replace(engine.get("ordinary"),
        meta={"id": "blend", "params": []}, execution_backend="composite", filter_fn=None,
        composite=CompositeSpec((CompositeChild("ordinary", 1.0), CompositeChild("matrix", 1.0))))
    blended = engine.run("blend", context)
    assert set(blended.scores) == set(ordinary.scores)
    assert set(blended.concept_heat_metadata["children"]) == {"ordinary", "matrix"}
    assert blended.concept_heat_metadata["mapping_version"] == ordinary.concept_heat_metadata["mapping_version"]


@pytest.mark.parametrize("backend", ["ordinary", "matrix"])
def test_engine_refuses_stale_latest_even_when_not_historical(tmp_path, monkeypatch, backend):
    engine, context = concept_engine(tmp_path, monkeypatch)
    stale = context.current.with_columns(pl.lit(date(2026, 9, 4)).alias("date"))
    with pytest.raises(ValueError, match="早于当前市场日期"):
        engine.run(backend, replace(context, current=stale))
    with pytest.raises(ValueError, match="可追溯的概念成分"):
        engine.run(backend, replace(context, is_historical=True))
    with pytest.raises(ValueError, match="多个交易日期"):
        engine.run(backend, replace(context, current=context.history))


@pytest.mark.parametrize("reference", ["scoring", "required", "filter", "order", "matrix", "parameter"])
def test_history_dependency_entries_reject_before_loading(tmp_path, monkeypatch, reference):
    from app.backtest.strategy import StrategyDependencyResolver

    engine, context = concept_engine(tmp_path, monkeypatch)
    strategy = replace(engine.get("ordinary"), meta={"id": "ordinary", "scoring": {"concept_heat": 0}})
    if reference == "scoring":
        strategy.meta["scoring"]["concept_heat"] = 1
    elif reference == "required":
        strategy.required_features = frozenset({"concept_heat"})
    elif reference == "filter":
        strategy.filter_fn = lambda frame, params: pl.col("concept_heat") > 0
    elif reference == "order":
        strategy.meta["order_by"] = "concept_heat"
    elif reference == "matrix":
        strategy.matrix_strategy = SimpleNamespace(required_fields=lambda: {"concept_heat"})
    else:
        strategy.matrix_strategy = SimpleNamespace(required_fields=lambda: {"close"},
            required_fields_for_params=lambda params: {params["factor"]})
    params = {"factor": "concept_heat"}
    engine._strategies["ordinary"] = strategy
    with pytest.raises(ValueError, match="可追溯的概念成分"):
        engine.run("ordinary", replace(context, is_historical=True), params=params)
    with pytest.raises(ValueError, match="可追溯的概念成分"):
        StrategyDependencyResolver().resolve(strategy, params=params, basic_filter={}, entry_signals=[], exit_signals=[])


def test_zero_weight_keeps_old_paths_without_concept_io(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    monkeypatch.setattr("app.strategy.engine.load_concept_mapping", lambda *args: pytest.fail("unused concept data read"))
    result = engine.run("ordinary", replace(context, is_historical=True), overrides={"scoring": {"concept_heat": 0}})
    assert result.total == 5
    assert result.concept_heat_metadata == {}


def test_inactive_parameter_branch_does_not_require_concept_mapping(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    engine.get("ordinary").filter_fn = lambda frame, params: pl.col("concept_heat") > 0 if params.get("use_heat") else pl.lit(True)
    monkeypatch.setattr("app.strategy.engine.load_concept_mapping", lambda *args: pytest.fail("unused concept data read"))
    result = engine.run("ordinary", replace(context, is_historical=True), overrides={"scoring": {"concept_heat": 0}})
    assert result.total == 5


def test_matrix_intraday_injection_never_broadcasts_to_prior_time(tmp_path, monkeypatch):
    from datetime import datetime

    from app.backtest.matrix import build_market_data_matrix

    engine, context = concept_engine(tmp_path, monkeypatch)
    current = context.current.with_columns(pl.lit(datetime(2026, 9, 10, 14, 0)).alias("datetime"))
    prior = current.with_columns(pl.lit(datetime(2026, 9, 10, 10, 0)).alias("datetime"))
    history = pl.concat([prior, current])
    market = build_market_data_matrix(history, field_columns={"change_pct", "concept_heat"})
    prepared = engine._with_concept_heat(replace(context, current=current, history=history, market=market), load_concept_mapping(tmp_path))
    assert np.isnan(prepared.market.fields["concept_heat"][0]).all()
    assert np.isfinite(prepared.market.fields["concept_heat"][1]).all()
    assert prepared.history.head(5)["concept_heat"].null_count() == 5


def test_composite_shares_one_formula_and_mapping_snapshot(tmp_path, monkeypatch):
    import app.strategy.engine as engine_module
    from app.strategy.engine import CompositeChild, CompositeSpec

    engine, context = concept_engine(tmp_path, monkeypatch)
    engine._strategies["blend"] = replace(engine.get("ordinary"),
        meta={"id": "blend", "params": []}, execution_backend="composite", filter_fn=None,
        composite=CompositeSpec((CompositeChild("ordinary", 1.0), CompositeChild("matrix", 1.0))))
    compute = engine_module.attach_concept_heat
    calls = []

    def record(frame, snapshot, **kwargs):
        calls.append(snapshot)
        return compute(frame, snapshot, **kwargs)

    monkeypatch.setattr(engine_module, "attach_concept_heat", record)
    result = engine.run("blend", context)
    assert result.total == 5
    assert len(calls) == 1


@pytest.mark.parametrize("prepared", [None, object()])
def test_backtest_rejects_current_snapshot_before_market_loading(tmp_path, monkeypatch, prepared):
    from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService

    engine, _context = concept_engine(tmp_path, monkeypatch)
    service = StrategyBacktestService(SimpleNamespace(), engine)
    result = service.run(StrategyBacktestConfig("matrix", SYMBOLS, DAY, DAY), prepared=prepared)
    assert "可追溯的概念成分" in result.error


def test_monitor_scope_uses_full_concept_membership(tmp_path, monkeypatch):
    from app.strategy.monitor import MonitorRuleEngine

    engine, context = concept_engine(tmp_path, monkeypatch)
    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: DAY)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(engine)
    monitor._data_dir = tmp_path
    monitor._match_strategy(context.current.head(1), {"strategy_id": "ordinary", "asset_type": "stock"}, full_frame=context.current)
    cached = monitor._building_strategy_results["ordinary"]
    assert cached["total"] == 1
    assert cached["rows"][0]["concept_heat"] == pytest.approx(.02)
    assert cached["concept_heat_metadata"]["input_symbols"] == 5
