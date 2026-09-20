"""独立验收: 固定市场时钟与临时映射, 不代表真实当日数据覆盖。"""
from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest
from test_concept_heat import DAY, SYMBOLS, AllEntries, concept_engine, quote_frame, write_mapping
from test_concept_heat_api import QuoteRepo, api_request

from app.api import screener as screener_api
from app.backtest.matrix import build_market_data_matrix, make_signal_matrix
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.services import strategy_cache
from app.strategy import concept_heat as concept_module
from app.strategy import config as strategy_config
from app.strategy.engine import CompositeChild, CompositeSpec
from app.strategy.monitor import MonitorRuleEngine

MARKET_SYMBOLS = {
    "stock": SYMBOLS,
    "hk": ["00700.HK", "00001.HK", "00005.HK", "00388.HK", "09988.HK"],
    "us": ["AAPL.US", "MSFT.US", "BRK.A.US", "BRK-B.US", "ABC1.US"],
    "etf": ["510300.SH", "510500.SH", "159915.SZ", "159919.SZ", "588000.SH"],
}
EXPECTED_HEAT = [.02, .03, .03, .01, .01]


def with_current(context, current):
    previous = current.with_columns(pl.lit(context.as_of - timedelta(days=1)).alias("date"))
    return replace(context, current=current, history=pl.concat([previous, current]))


def add_composite(engine):
    original = engine.get("ordinary")
    engine._strategies["blend"] = replace(
        original,
        meta={**original.meta, "id": "blend", "scoring": {"concept_heat": 0}},
        execution_backend="composite",
        filter_fn=None,
        composite=CompositeSpec((CompositeChild("ordinary", 1), CompositeChild("matrix", 1))),
    )


class PriceEntries(AllEntries):
    def compute_signals(self, market, params):
        return make_signal_matrix(market.shape, entry=(market.close >= 9).astype(np.uint8))


@pytest.mark.parametrize("candidate_filter", ["pool", "basic", "strategy"])
def test_candidate_filters_do_not_remove_contributors_to_heat(tmp_path, monkeypatch, candidate_filter):
    """最终只选 A, 仍须使用被过滤的 B/C/D/E 得到两个概念均值 .02。"""
    engine, context = concept_engine(tmp_path, monkeypatch)
    current = context.current.with_columns(
        *(pl.Series(field, [10., 8., 8., 6., 6.]) for field in ("open", "high", "low", "close"))
    )
    context = with_current(context, current)
    kwargs = {}
    if candidate_filter == "pool":
        kwargs["pool"] = [SYMBOLS[0]]
    elif candidate_filter == "basic":
        kwargs["overrides"] = {"basic_filter": {"enabled": True, "price_min": 9}}
    else:
        engine.get("ordinary").filter_fn = lambda frame, params: pl.col("close") >= 9
        engine.get("matrix").matrix_strategy = PriceEntries()
    market = build_market_data_matrix(context.history, field_columns={"change_pct"})
    original_close = market.close.copy()
    results = [
        engine.run("ordinary", context, **kwargs),
        engine.run("matrix", context, **kwargs),
        engine.run("matrix", replace(context, market=market), **kwargs),
    ]
    for result in results:
        assert [row["symbol"] for row in result.rows] == [SYMBOLS[0]]
        assert result.rows[0]["concept_heat"] == pytest.approx(.02)
        assert result.scores[SYMBOLS[0]] == pytest.approx(50.)
        assert result.concept_heat_metadata["input_symbols"] == 5
    assert "concept_heat" not in market.fields
    np.testing.assert_array_equal(market.close, original_close)
    assert "concept_heat" not in context.current.columns
    assert "concept_heat" not in context.history.columns


@pytest.mark.parametrize("direction", ["high", "low"])
def test_partial_coverage_has_explicit_exclusions_and_consistent_rankings(tmp_path, monkeypatch, direction):
    engine, context = concept_engine(tmp_path, monkeypatch)
    path = tmp_path / "ext_data" / "ext_gn_ths" / "part.parquet"
    mapping = pl.read_parquet(path)
    pl.concat([mapping, pl.DataFrame({"symbol": ["600006.SH"], "所属概念": ["单成员"]})]).write_parquet(path)
    extra = pl.concat([
        context.current.head(1).with_columns(pl.lit(symbol).alias("symbol"))
        for symbol in ("600006.SH", "600007.SH")
    ])
    context = with_current(context, pl.concat([context.current, extra]))
    overrides = {"scoring_directions": {"concept_heat": direction}}
    results = engine.run_all(context, strategy_ids=["ordinary", "matrix"],
                             overrides_map=dict.fromkeys(("ordinary", "matrix"), overrides))
    expected_scores = dict(zip(SYMBOLS, [50., 100., 100., 0., 0.], strict=True))
    if direction == "low":
        expected_scores = {symbol: 100 - score for symbol, score in expected_scores.items()}
    for result in results.values():
        assert result.scores == pytest.approx(expected_scores, abs=1e-5)
        assert {row["symbol"]: row["concept_heat"] for row in result.rows} == pytest.approx(
            dict(zip(SYMBOLS, EXPECTED_HEAT, strict=True))
        )
        assert any("2 个标的缺少有效 concept_heat" in warning for warning in result.warnings)
        assert result.concept_heat_metadata["status"] == "partial"
        assert result.concept_heat_metadata["input_symbols"] == 7
        assert result.concept_heat_metadata["mapped_symbols"] == 6
        assert result.concept_heat_metadata["computable_symbols"] == 5
        assert result.concept_heat_metadata["unmapped_symbols"] == 1
        assert result.concept_heat_metadata["missing_valid_concept_symbols"] == 1
    column = concept_module.concept_scoring_column(QuoteRepo(tmp_path, frame=context.current))
    assert column["available"] is True
    assert column["metadata"]["status"] == "partial"
    add_composite(engine)
    merged = engine.run("blend", context)
    assert set(merged.scores) == set(SYMBOLS)
    assert merged.concept_heat_metadata["status"] == "partial"
    assert set(merged.concept_heat_metadata["children"]) == {"ordinary", "matrix"}
    assert any("2 个标的缺少有效 concept_heat" in warning for warning in merged.warnings)


@pytest.mark.parametrize("asset_type", ["stock", "hk", "us"])
def test_synthetic_three_market_paths_use_each_market_clock(tmp_path, monkeypatch, asset_type):
    engine, context = concept_engine(tmp_path, monkeypatch)
    symbols = MARKET_SYMBOLS[asset_type]
    market = "cn" if asset_type == "stock" else asset_type
    clock_dates = {"cn": DAY, "hk": DAY, "us": DAY - timedelta(days=1)}
    calls = []

    def profile(name):
        calls.append(name.lower())
        return SimpleNamespace(today=lambda: clock_dates[name.lower()])

    monkeypatch.setattr("app.markets.get_profile", profile)
    path = tmp_path / "ext_data" / "ext_gn_ths" / "part.parquet"
    pl.read_parquet(path).with_columns(pl.Series("symbol", symbols)).write_parquet(path)
    day = clock_dates[market]
    current = context.current.with_columns(pl.Series("symbol", symbols), pl.lit(day).alias("date"))
    context = with_current(replace(context, asset_type=asset_type, as_of=day), current)
    results = engine.run_all(context, strategy_ids=["ordinary", "matrix"])
    for result in results.values():
        assert {row["symbol"]: row["concept_heat"] for row in result.rows} == pytest.approx(
            dict(zip(symbols, EXPECTED_HEAT, strict=True))
        )
        assert result.concept_heat_metadata["market"] == market
        assert result.concept_heat_metadata["quote_date"] == str(day)
        assert result.concept_heat_metadata["current_market_date"] == str(day)
    assert results["ordinary"].scores == pytest.approx(results["matrix"].scores)
    column = concept_module.concept_scoring_column(
        QuoteRepo(tmp_path, frame=current, latest=day), asset_type=asset_type
    )
    assert column["available"] is True
    assert calls and set(calls) == {market}


def test_same_named_concepts_do_not_mix_market_or_intraday_slices(tmp_path):
    directory = write_mapping(tmp_path)
    mapping = pl.read_parquet(directory / "part.parquet")
    foreign = mapping.with_columns(pl.Series("symbol", MARKET_SYMBOLS["us"]))
    pl.concat([mapping, foreign]).write_parquet(directory / "part.parquet")
    pieces = []
    for hour, multiplier, symbols in ((10, 1, SYMBOLS), (14, -1, SYMBOLS), (10, 10, MARKET_SYMBOLS["us"])):
        pieces.append(quote_frame().with_columns(
            pl.Series("symbol", symbols),
            pl.lit(datetime(2026, 9, 10, hour)).alias("datetime"),
            (pl.col("change_pct") * multiplier).alias("change_pct"),
        ))
    frame = pl.concat(pieces).with_row_index("original_row")
    for market, expected in (
        ("cn", [*EXPECTED_HEAT, *[-value for value in EXPECTED_HEAT], *([None] * 5)]),
        ("us", [*([None] * 10), *[value * 10 for value in EXPECTED_HEAT]]),
    ):
        result = concept_module.attach_concept_heat(
            frame, concept_module.load_concept_mapping(tmp_path, market), market=market,
        )
        assert result["original_row"].to_list() == list(range(15))
        assert result["concept_heat"].to_list() == pytest.approx(expected)


def test_missing_own_return_can_use_three_other_members_without_duplicate_votes(tmp_path):
    directory = write_mapping(tmp_path, concepts=["共同"] * 5)
    mapping = pl.read_parquet(directory / "part.parquet")
    pl.concat([mapping, mapping.head(1)]).write_parquet(directory / "part.parquet")
    snapshot = concept_module.load_concept_mapping(tmp_path)
    frame = quote_frame().with_columns(pl.Series("change_pct", [.02, .04, .06, None, float("inf")]))
    result = concept_module.attach_concept_heat(frame, snapshot)
    assert result["concept_heat"].to_list() == pytest.approx([.04] * 5)
    duplicate_only = pl.concat([frame.head(2), frame.head(2), frame.head(1)])
    result = concept_module.attach_concept_heat(duplicate_only, snapshot)
    assert result["concept_heat"].null_count() == duplicate_only.height


@pytest.mark.parametrize("asset_type", ["stock", "etf", "hk", "us"])
def test_replacing_scoring_removes_concept_dependency_for_old_paths(tmp_path, monkeypatch, asset_type):
    engine, context = concept_engine(tmp_path, monkeypatch)
    symbols = MARKET_SYMBOLS[asset_type]
    current = context.current.with_columns(pl.Series("symbol", symbols))
    context = with_current(replace(context, asset_type=asset_type, is_historical=True), current)

    def forbidden(*args, **kwargs):
        pytest.fail("已替换评分且没有概念依赖的策略不应读取概念文件")

    monkeypatch.setattr(concept_module, "load_concept_mapping", forbidden)
    monkeypatch.setattr("app.strategy.engine.load_concept_mapping", forbidden)
    overrides = {"scoring_replace": True, "scoring": {"close": 1, "concept_heat": 0}}
    results = engine.run_all(context, strategy_ids=["ordinary", "matrix"],
                             overrides_map=dict.fromkeys(("ordinary", "matrix"), overrides))
    for result in results.values():
        assert set(result.scores) == set(symbols)
        assert result.concept_heat_metadata == {}


@pytest.mark.parametrize("prepared", [None, object()])
def test_parent_zero_weight_cannot_hide_historical_child_dependency(tmp_path, monkeypatch, prepared):
    engine, context = concept_engine(tmp_path, monkeypatch)
    add_composite(engine)

    def forbidden(*args, **kwargs):
        pytest.fail("历史拒绝必须先于概念源读取")

    monkeypatch.setattr("app.strategy.engine.load_concept_mapping", forbidden)
    with pytest.raises(ValueError, match="可追溯的概念成分"):
        engine.run_all(replace(context, is_historical=True), strategy_ids=["blend"])
    service = StrategyBacktestService(SimpleNamespace(), engine)
    result = service.run(StrategyBacktestConfig("blend", SYMBOLS, DAY, DAY), prepared=prepared)
    assert "可追溯的概念成分" in result.error


@pytest.mark.parametrize("change_forever", [False, True])
def test_mapping_read_retries_or_rejects_changes_without_mislabelling_pairs(tmp_path, monkeypatch, change_forever):
    directory = write_mapping(tmp_path)
    path = directory / "part.parquet"
    original_read = pl.read_parquet
    read_count = 0

    def racing_read(source, *args, **kwargs):
        nonlocal read_count
        frame = original_read(source, *args, **kwargs)
        read_count += 1
        if change_forever or read_count == 1:
            frame.with_columns(pl.lit(f"新版本{read_count}").alias("所属概念")).write_parquet(path)
        return frame

    with monkeypatch.context() as patcher:
        patcher.setattr(concept_module.pl, "read_parquet", racing_read)
        snapshot = concept_module.load_concept_mapping(tmp_path)
    if change_forever:
        assert snapshot.status == "source_changed"
        assert snapshot.pairs.is_empty()
        assert concept_module.load_concept_mapping(tmp_path).pairs["concept"].unique().to_list() == ["新版本2"]
    else:
        assert snapshot.status == "available"
        assert snapshot.pairs["concept"].unique().to_list() == ["新版本1"]
        assert concept_module.load_concept_mapping(tmp_path).version == snapshot.version


def test_config_only_source_update_invalidates_scores_and_ever_hits(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old_snapshot = concept_module.load_concept_mapping(tmp_path)
    old_result = asdict(engine.run("ordinary", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old_result})
    path = tmp_path / "ext_data" / "ext_gn_ths" / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["updated_at"] = "2026-09-10T15:12:45.123456+08:00"
    path.write_text(json.dumps(config), encoding="utf-8")
    snapshot = concept_module.load_concept_mapping(tmp_path)
    assert snapshot.version != old_snapshot.version
    assert snapshot.pairs.equals(old_snapshot.pairs)
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine))
    assert cached["results"]["ordinary"]["rows"] == []
    assert cached["results"]["ordinary"]["concept_heat_metadata"]["status"] == "unavailable"
    assert "ordinary" not in cached["today_ever_rows"]


@pytest.mark.parametrize("change", ["weights", "remove"])
def test_new_valid_overlay_drops_ever_hits_from_old_scoring(tmp_path, monkeypatch, change):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old = asdict(engine.run("ordinary", context, pool=[SYMBOLS[0]]))
    strategy_cache.write_cache(tmp_path, str(DAY), {
        "ordinary": old, "unrelated": {"rows": [{"symbol": "OTHER"}]},
    })
    overrides = {"scoring": {"concept_heat": .25, "close": .75}}
    if change == "remove":
        overrides = {"scoring_replace": True, "scoring": {"close": 1}}
    strategy_config.save_override(tmp_path, "ordinary", overrides)
    new = asdict(engine.run("ordinary", context, pool=[SYMBOLS[1]], overrides=overrides))
    monitor = SimpleNamespace(latest_strategy_results=lambda: {"ordinary": new})
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine, monitor))
    assert [row["symbol"] for row in cached["results"]["ordinary"]["rows"]] == [SYMBOLS[1]]
    assert SYMBOLS[0] not in cached.get("today_ever_rows", {}).get("ordinary", {})
    assert SYMBOLS[0] not in cached.get("today_ever_matched", {}).get("ordinary", [])
    assert set(cached["today_ever_rows"]["unrelated"]) == {"OTHER"}


def test_same_version_realtime_overlay_keeps_valid_ever_hits(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old = asdict(engine.run("ordinary", context, pool=[SYMBOLS[0]]))
    new = asdict(engine.run("ordinary", context, pool=[SYMBOLS[1]]))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    monitor = SimpleNamespace(latest_strategy_results=lambda: {"ordinary": new})
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine, monitor))
    assert cached["results"]["ordinary"]["rows"][0]["symbol"] == SYMBOLS[1]
    assert SYMBOLS[0] in cached["today_ever_rows"]["ordinary"]


def test_monitor_public_evaluate_reports_loss_of_concept_capability(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: DAY)
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(engine)
    monitor.set_data_dir(tmp_path)
    monitor.set_rules([{
        "id": "qa_scope", "type": "strategy", "strategy_id": "ordinary", "asset_type": "stock",
        "scope": "symbols", "symbols": [SYMBOLS[0]], "cooldown_seconds": 0,
    }])
    monitor.evaluate(context.current)
    current = monitor.latest_strategy_results()["ordinary"]
    assert current["total"] == 1
    assert current["rows"][0]["concept_heat"] == pytest.approx(.02)
    assert current["concept_heat_metadata"]["input_symbols"] == 5
    assert monitor.consume_strategy_result_updates() is True
    monitor.evaluate(context.current.head(2))
    unavailable = monitor.latest_strategy_results()["ordinary"]
    assert unavailable["rows"] == []
    assert unavailable["concept_heat_metadata"]["status"] == "unavailable"
    assert "3 个独立有效成员" in unavailable["warnings"][0]
    assert monitor.consume_strategy_result_updates() is True
