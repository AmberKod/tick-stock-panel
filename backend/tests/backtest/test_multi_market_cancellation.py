"""Cancellation must follow the requested market across data generations."""
from __future__ import annotations

from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as api


@pytest.mark.parametrize("target_market", ["hk", "us"])
def test_cancel_isolates_market_and_completed_jobs_across_generations(monkeypatch, target_market):
    def request_key(market):
        return api._make_job_key(
            "shared_strategy", None, "2024-01-02", "2024-01-05", "open_t+1",
            None, None, None, 5.0, 10, 1.0, 1_000_000.0, "equal", None, None,
            asset_type=market,
        )

    other_market = "us" if target_market == "hk" else "hk"
    target_key = request_key(target_market)
    jobs = {
        "previous_generation": api._BacktestJob("previous", request_key=target_key),
        "current_generation": api._BacktestJob("current", request_key=target_key),
        "completed": api._BacktestJob("completed", request_key=target_key),
        "other_market": api._BacktestJob("other", request_key=request_key(other_market)),
    }
    jobs["completed"].done = True
    monkeypatch.setattr(api, "_running_jobs", jobs)
    app = FastAPI()
    app.include_router(api.router)

    with TestClient(app) as client:
        response = client.post("/api/backtest/strategy/cancel", json={"qs": urlencode({
            "strategy_id": "shared_strategy", "asset_type": target_market,
            "start": "2024-01-02", "end": "2024-01-05",
        })})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "cancelled_count": 2}
    assert jobs["previous_generation"].cancel_event.is_set()
    assert jobs["current_generation"].cancel_event.is_set()
    assert not jobs["completed"].cancel_event.is_set()
    assert not jobs["other_market"].cancel_event.is_set()
