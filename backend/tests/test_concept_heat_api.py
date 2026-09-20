"""HTTP/catalog and cache lifecycle tests use isolated synthetic current data."""
from dataclasses import asdict, replace
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_concept_heat import DAY, SYMBOLS, concept_engine, quote_frame, write_mapping

from app.api import backtest as backtest_api
from app.api import screener as screener_api
from app.api import strategy as strategy_api
from app.backtest.factor import FACTOR_COLUMNS
from app.services import strategy_cache
from app.strategy import config as strategy_config
from app.strategy.concept_heat import concept_scoring_column


class QuoteRepo:
    def __init__(self, data_dir, frame=None, latest=DAY):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.frame = frame if frame is not None else quote_frame()
        self.latest = latest

    def enriched_latest_date(self):
        return self.latest

    def get_enriched_latest_asset(self, asset_type, refresh=True):
        return self.frame, self.latest


def api_request(data_dir, engine, monitor=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=QuoteRepo(data_dir), strategy_engine=engine, monitor_engine=monitor,
    )))


def test_factor_catalog_keeps_historical_research_whitelist(tmp_path, monkeypatch):
    engine, _context = concept_engine(tmp_path, monkeypatch)
    app = FastAPI()
    app.state.repo = QuoteRepo(tmp_path)
    app.state.strategy_engine = engine
    app.include_router(backtest_api.router)
    with TestClient(app) as client:
        assert client.get("/api/backtest/factor/columns").json() == {"columns": FACTOR_COLUMNS}
        response = client.get("/api/backtest/factor/columns", params={"purpose": "scoring", "as_of": str(DAY)})
        assert response.status_code == 200
        concept = next(item for item in response.json()["columns"] if item["id"] == "concept_heat")
        assert concept["available"] is True
        assert concept["metadata"]["valid_concepts"] == 2
        assert concept["metadata"]["quote_date"] == str(DAY)
        historical = client.get("/api/backtest/factor/columns", params={"purpose": "scoring", "context": "historical"}).json()["columns"][-1]
        assert historical["available"] is False
        assert historical["metadata"]["reason_code"] == "historical_membership"
        assert client.get("/api/backtest/factor/columns", params={"purpose": "invalid"}).status_code == 422
        result = client.post("/api/backtest/factor/run", json={"factor_name": "concept_heat"})
        assert result.status_code == 400
    assert "concept_heat" not in {factor["id"] for factor in FACTOR_COLUMNS}


@pytest.mark.parametrize("market", ["hk", "us", "etf"])
def test_scoring_catalog_explains_missing_market_mapping(tmp_path, monkeypatch, market):
    concept_engine(tmp_path, monkeypatch)
    repo = QuoteRepo(tmp_path, latest=date(2026, 9, 4))
    column = concept_scoring_column(repo, asset_type=market)
    assert column["available"] is False
    assert column["metadata"]["reason_code"] == ("unsupported_asset" if market == "etf" else "missing_mapping")


def test_catalog_validates_actual_quote_dates_and_member_overlap(tmp_path, monkeypatch):
    concept_engine(tmp_path, monkeypatch)
    stale = quote_frame().with_columns(pl.lit(date(2026, 9, 4)).alias("date"))
    column = concept_scoring_column(QuoteRepo(tmp_path, frame=stale))
    assert column["available"] is False
    assert column["metadata"]["reason_code"] == "stale_quote"
    for frame in (quote_frame().head(2), quote_frame().with_columns(pl.lit("999999.SZ").alias("symbol"))):
        column = concept_scoring_column(QuoteRepo(tmp_path, frame=frame))
        assert column["available"] is False
        assert column["metadata"]["reason_code"] == "insufficient_members"
    column = concept_scoring_column(QuoteRepo(tmp_path, frame=quote_frame().drop("change_pct")))
    assert column["metadata"]["reason_code"] == "missing_change_pct"


def test_catalog_reports_corrupt_and_missing_sources(tmp_path, monkeypatch):
    concept_engine(tmp_path, monkeypatch)
    (tmp_path / "ext_data" / "ext_gn_ths" / "part.parquet").write_bytes(b"bad parquet")
    assert concept_scoring_column(QuoteRepo(tmp_path))["metadata"]["reason_code"] == "read_error"
    assert concept_scoring_column(QuoteRepo(tmp_path / "missing"))["metadata"]["reason_code"] == "missing_source"


def test_valid_cache_roundtrip_preserves_metadata_and_results(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    result = asdict(engine.run("ordinary", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": result})
    request = api_request(tmp_path, engine)
    cached = screener_api._cached_with_realtime(request)
    assert cached["results"]["ordinary"]["total"] == 5
    assert cached["results"]["ordinary"]["concept_heat_metadata"] == result["concept_heat_metadata"]
    assert len(cached["today_ever_rows"]["ordinary"]) == 5
    summary = screener_api.get_cached_summary(request)
    assert summary["results"]["ordinary"]["concept_heat_metadata"]["mapping_version"] == result["concept_heat_metadata"]["mapping_version"]
    single = screener_api.get_cached_result("ordinary", request, ext_columns=None)
    assert single["result"]["concept_heat_metadata"] == result["concept_heat_metadata"]


@pytest.mark.parametrize("change", ["mapping", "day", "market", "metadata", "quote", "config"])
def test_cached_overlay_invalidates_result_and_ever_hits(tmp_path, monkeypatch, change):
    engine, context = concept_engine(tmp_path, monkeypatch)
    result = asdict(engine.run("ordinary", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": result})
    overlay = {**result, "concept_heat_metadata": dict(result["concept_heat_metadata"])}
    if change == "mapping":
        write_mapping(tmp_path, concepts=["更新"] * 5)
    elif change == "day":
        monkeypatch.setattr("app.markets.get_profile", lambda market: SimpleNamespace(today=lambda: date(2026, 9, 11)))
    elif change == "market":
        overlay["concept_heat_metadata"]["market"] = "us"
    elif change == "metadata":
        overlay.pop("concept_heat_metadata")
    elif change == "quote":
        overlay["concept_heat_metadata"]["quote_date"] = "2026-09-04"
    else:
        strategy_config.save_override(tmp_path, "ordinary", {"scoring": {"concept_heat": .8, "close": .2}})
    monitor = SimpleNamespace(latest_strategy_results=lambda: {"ordinary": overlay})
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine, monitor))
    invalid = cached["results"]["ordinary"]
    assert invalid["rows"] == [] and invalid["total"] == 0
    assert invalid["concept_heat_metadata"]["status"] == "unavailable"
    assert invalid["warnings"]
    assert "ordinary" not in cached["today_ever_rows"]
    assert "ordinary" not in cached["today_ever_matched"]


@pytest.mark.parametrize("method", ["save", "patch", "reset"])
def test_config_writes_clear_runtime_and_reject_late_old_result(tmp_path, monkeypatch, method):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old = asdict(engine.run("ordinary", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    invalidations = []
    monitor = SimpleNamespace(invalidate_strategy_state=lambda: invalidations.append(True), latest_strategy_results=lambda: {})
    request = api_request(tmp_path, engine, monitor)
    if method == "reset":
        strategy_config.save_override(tmp_path, "ordinary", {"scoring": {"concept_heat": .8, "close": .2}})
        old = asdict(engine.run("ordinary", context, overrides=strategy_config.load_override(tmp_path, "ordinary")))
        strategy_api.reset_config("ordinary", request)
    else:
        operation = strategy_api.save_config if method == "save" else strategy_api.patch_config
        operation(strategy_api.SaveConfigRequest(strategy_id="ordinary", overrides={"scoring": {"concept_heat": .8, "close": .2}}), request)
        assert strategy_config.load_override(tmp_path, "ordinary")["scoring"]["concept_heat"] == .8
    assert invalidations == [True]
    assert strategy_cache.read_cache(tmp_path) is None
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    assert screener_api._cached_with_realtime(request)["results"]["ordinary"]["total"] == 0


def test_new_mapping_or_weight_resets_only_related_same_day_union(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old = asdict(engine.run("ordinary", context, pool=[SYMBOLS[0]]))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old, "unrelated": {"rows": [{"symbol": "OTHER"}]}})
    write_mapping(tmp_path, concepts=["新概念"] * 5)
    new = asdict(engine.run("ordinary", context, pool=[SYMBOLS[1]]))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": new})
    cached = strategy_cache.read_cache(tmp_path)
    assert set(cached["today_ever_rows"]["ordinary"]) == {SYMBOLS[1]}
    assert set(cached["today_ever_rows"]["unrelated"]) == {"OTHER"}


def test_fresh_mapping_overlay_cannot_inherit_previous_mapping_ever_hits(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    old = asdict(engine.run("ordinary", context, pool=[SYMBOLS[0]]))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    write_mapping(tmp_path, concepts=["更新后的映射"] * 5)
    fresh = asdict(engine.run("ordinary", context, pool=[SYMBOLS[1]]))
    assert fresh["concept_heat_metadata"]["mapping_version"] != old["concept_heat_metadata"]["mapping_version"]
    monitor = SimpleNamespace(latest_strategy_results=lambda: {"ordinary": fresh})
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine, monitor))
    assert cached["results"]["ordinary"]["total"] == 1
    assert cached["results"]["ordinary"]["rows"][0]["symbol"] == SYMBOLS[1]
    assert cached["results"]["ordinary"]["concept_heat_metadata"]["status"] == "available"
    assert SYMBOLS[0] not in cached["today_ever_rows"].get("ordinary", {})
    assert SYMBOLS[0] not in cached["today_ever_matched"].get("ordinary", [])


def test_unused_concept_cache_does_not_read_source(tmp_path, monkeypatch):
    engine, context = concept_engine(tmp_path, monkeypatch)
    engine.get("ordinary").meta["scoring"] = {"concept_heat": 0}
    old = asdict(engine.run("ordinary", replace(context, is_historical=True)))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    monkeypatch.setattr("app.strategy.concept_heat.load_concept_mapping", lambda *args: pytest.fail("unused source read"))
    assert screener_api._cached_with_realtime(api_request(tmp_path, engine))["results"]["ordinary"]["total"] == 5


def test_composite_cache_fingerprint_tracks_saved_child_configuration(tmp_path, monkeypatch):
    from app.strategy.engine import CompositeChild, CompositeSpec

    engine, context = concept_engine(tmp_path, monkeypatch)
    engine._override_loader = lambda sid: strategy_config.load_override(tmp_path, sid)
    engine._strategies["blend"] = replace(engine.get("ordinary"),
        meta={"id": "blend", "params": []}, execution_backend="composite", filter_fn=None,
        composite=CompositeSpec((CompositeChild("ordinary", 1.0), CompositeChild("matrix", 1.0))))
    result = asdict(engine.run("blend", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"blend": result})
    request = api_request(tmp_path, engine)
    assert screener_api._cached_with_realtime(request)["results"]["blend"]["total"] == 5
    strategy_config.save_override(tmp_path, "ordinary", {"scoring": {"concept_heat": .2, "close": .8}})
    assert screener_api._cached_with_realtime(request)["results"]["blend"]["total"] == 0


def test_historical_screener_rejects_before_loading_history(tmp_path, monkeypatch):
    from app.services.screener import ScreenerService

    engine, _context = concept_engine(tmp_path, monkeypatch)
    service = ScreenerService(QuoteRepo(tmp_path))
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *args: pytest.fail("historical quotes were loaded"))
    with pytest.raises(ValueError, match="可追溯的概念成分"):
        service.build_strategy_context(engine, date(2026, 9, 9), ["ordinary"])


def test_monitor_failure_does_not_fall_back_to_old_scores_or_hide_reason(tmp_path, monkeypatch):
    from app.strategy.monitor import MonitorRuleEngine

    engine, context = concept_engine(tmp_path, monkeypatch)
    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: DAY)
    old = asdict(engine.run("ordinary", context))
    strategy_cache.write_cache(tmp_path, str(DAY), {"ordinary": old})
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(engine)
    monitor._data_dir = tmp_path
    monitor._match_strategy(context.current.head(2), {"strategy_id": "ordinary"})
    monitor._latest_strategy_results = monitor._building_strategy_results
    cached = screener_api._cached_with_realtime(api_request(tmp_path, engine, monitor))
    assert cached["results"]["ordinary"]["rows"] == []
    assert "3 个独立有效成员" in cached["results"]["ordinary"]["warnings"][0]
    assert "ordinary" not in cached["today_ever_rows"]
