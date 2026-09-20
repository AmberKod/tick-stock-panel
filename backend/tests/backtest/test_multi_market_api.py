"""POST, stream, worker and cancellation must describe the same market task."""
from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as api


@pytest.fixture
def client_and_tasks(monkeypatch):
    from app.backtest import worker

    tasks = []
    generation = {"us": "us-v1", "hk": "hk-v1"}

    def run(task, *args, **kwargs):
        tasks.append(task)
        return {"config": task["config"], "stats": {}, "trades": []}

    monkeypatch.setattr(worker, "run_worker_task", run)
    monkeypatch.setattr(api.settings, "backtest_range_guard", False)
    monkeypatch.setattr(api, "_running_jobs", {})
    app = FastAPI()
    app.state.repo = SimpleNamespace(get_matrix_data_generation=lambda market: generation[market])
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client, tasks, generation


def params():
    return {
        "strategy_id": "ma_golden_cross", "symbols": "BRK.A.US", "asset_type": "us",
        "start": "2024-01-02", "end": "2024-01-05", "commission_pct": 0.001,
        "buy_stamp_tax_pct": 0.002, "stamp_tax_pct": 0.003,
    }


def test_post_and_sse_pass_identical_market_costs_to_worker(client_and_tasks):
    client, tasks, _ = client_and_tasks
    request = params()
    response = client.post("/api/backtest/strategy/run", json={**request, "symbols": ["BRK.A.US"]})
    assert response.status_code == 200, response.text
    streamed = client.get("/api/backtest/strategy/stream", params=request)
    assert streamed.status_code == 200, streamed.text
    assert "event: done" in streamed.text
    assert len(tasks) == 2
    assert tasks[0]["config"] == tasks[1]["config"]
    assert tasks[1]["config"]["fees_pct"] == 0
    assert tasks[1]["config"]["buy_stamp_tax_pct"] == 0.002


def test_stream_reuses_same_generation_and_refreshes_changed_data(client_and_tasks):
    client, tasks, generation = client_and_tasks
    request = params()
    assert "event: done" in client.get("/api/backtest/strategy/stream", params=request).text
    assert "event: done" in client.get("/api/backtest/strategy/stream", params=request).text
    assert len(tasks) == 1
    generation["us"] = "us-v2"
    assert "event: done" in client.get("/api/backtest/strategy/stream", params=request).text
    assert len(tasks) == 2


@pytest.mark.parametrize("patch", [
    {"symbols": "00700.HK"}, {"minute_fill": "true"},
    {"exit_fill": "signal_next_minute"}, {"buy_stamp_tax_pct": -0.001},
    {"fees_pct": "nan"}, {"asset_type": "unexpected"},
])
def test_stream_rejects_invalid_execution_before_starting_a_job(client_and_tasks, patch):
    client, tasks, _ = client_and_tasks
    response = client.get("/api/backtest/strategy/stream", params={**params(), **patch})
    assert response.status_code == 422
    assert tasks == []
    assert api._running_jobs == {}


def test_cancel_matches_buy_tax_market_and_regime_parameters(client_and_tasks):
    client, _, _ = client_and_tasks
    request = params()
    request.update({"asset_type": "stock", "symbols": "600000.SH", "minute_fill": "true", "regime_filter": json.dumps({"states": ["strong"]})})
    key = api._make_job_key(
        request["strategy_id"], request["symbols"], request["start"], request["end"],
        "open_t+1", None, None, None, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None,
        commission_pct=0.001, stamp_tax_pct=0.003, buy_stamp_tax_pct=0.002,
        asset_type="stock", minute_fill=True, regime_filter=request["regime_filter"],
    )
    job = api._BacktestJob(key)
    api._running_jobs[key] = job
    response = client.post("/api/backtest/strategy/cancel", json={"qs": urlencode(request)})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert job.cancel_event.is_set()
