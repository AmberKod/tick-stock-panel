from __future__ import annotations

from types import SimpleNamespace

import polars as pl

from app.api import screener as screener_api
from app.strategy.portfolio_constraints import industry_mapping_version


class _MonitorEngine:
    def __init__(self, results=None):
        self.results = results or {}

    def latest_strategy_results(self):
        return self.results


def _request(tmp_path, monitor_results=None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, monitor_engine=_MonitorEngine(monitor_results))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_cached_summary_omits_rows_and_counts_realtime_expirations(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "000002.SZ"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "600000.SH": {"symbol": "600000.SH"},
            },
        },
        "updated_at": 1,
    }
    realtime = {
        "strategy_a": {
            "as_of": "2026-07-20",
            "total": 2,
            "rows": [{"symbol": "000002.SZ"}, {"symbol": "300001.SZ"}],
        },
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)

    payload = screener_api.get_cached_summary(_request(tmp_path, realtime))

    assert payload["results"] == {"strategy_a": {"total": 2, "as_of": "2026-07-20"}}
    assert payload["today_ever_counts"] == {"strategy_a": 4}
    assert "rows" not in payload["results"]["strategy_a"]


def test_cached_result_returns_only_requested_rows_with_ext_and_strategy_membership(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 1,
                "rows": [{"symbol": "000001.SZ"}],
            },
            "strategy_b": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "000002.SZ": {"symbol": "000002.SZ"},
            },
        },
        "updated_at": 1,
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    monkeypatch.setattr(
        screener_api,
        "_load_ext_value_maps",
        lambda *_args: {"concept.concept": {"000001.SZ": "银行", "000002.SZ": "科技"}},
    )

    payload = screener_api.get_cached_result(
        "strategy_a",
        _request(tmp_path),
        ext_columns="concept.concept",
    )

    assert payload["result"]["strategy"] == "strategy_a"
    assert payload["result"]["rows"] == [{"symbol": "000001.SZ", "concept.concept": "银行"}]
    assert payload["today_ever_rows"]["000002.SZ"]["concept.concept"] == "科技"
    assert payload["strategy_ids_by_symbol"] == {"000001.SZ": ["strategy_a", "strategy_b"]}


def test_cached_diagnostics_survive_single_result_and_summary(monkeypatch, tmp_path):
    warnings = ["1 个标的缺少行业热度,无法评分,已排除"]
    cached = {"as_of": "2026-09-08", "results": {
        "heat": {"as_of": "2026-09-08", "rows": [], "total": 0, "warnings": warnings},
    }}
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    monkeypatch.setattr(screener_api, "_load_ext_value_maps", lambda *_args: {})
    request = _request(tmp_path)
    assert screener_api.get_cached_summary(request)["results"]["heat"]["warnings"] == warnings
    assert screener_api.get_cached_result("heat", request)["result"]["warnings"] == warnings


def test_mapping_change_invalidates_only_dependent_cached_strategies(monkeypatch, tmp_path):
    directory = tmp_path / "ext_data" / "ext_hy_ths"
    directory.mkdir(parents=True)
    path = directory / "part.parquet"
    pl.DataFrame({"symbol": ["000001.SZ"], "industry": ["银行"]}).write_parquet(path)
    cached = {"as_of": "2026-09-08", "results": {
        "heat": {"as_of": "2026-09-08", "rows": [], "total": 0,
                 "industry_mapping_version": industry_mapping_version(tmp_path)},
        "price": {"as_of": "2026-09-08", "rows": [], "total": 0},
    }}
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    request = _request(tmp_path)
    assert set(screener_api.get_cached_summary(request)["results"]) == {"heat", "price"}
    pl.DataFrame({"symbol": ["000001.SZ"], "industry": ["软件服务"]}).write_parquet(path)
    assert set(screener_api.get_cached_summary(request)["results"]) == {"price"}


def test_realtime_overlay_retains_diagnostics_and_mapping_version(monkeypatch, tmp_path):
    version = industry_mapping_version(tmp_path)
    warnings = ["1 个标的缺少行业热度, 无法评分, 已排除"]
    cached = {"as_of": "2026-09-08", "results": {}}
    realtime = {"heat": {"as_of": "2026-09-08", "rows": [], "total": 0,
                         "warnings": warnings, "industry_mapping_version": version}}
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    result = screener_api.get_cached_summary(_request(tmp_path, realtime))["results"]["heat"]
    assert result["warnings"] == warnings
    assert result["industry_mapping_version"] == version
