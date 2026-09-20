"""Hotspot routes through the real application and authentication middleware.

All data, passwords, logs and sessions belong to temporary test directories.
The application lifespan is replaced only in these tests to avoid schedulers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_HOTSPOT_REQUESTS = (
    ("GET", "/api/v1/hotspots?market=cn"),
    ("GET", "/api/v1/hotspots/人工智能?market=cn"),
    ("POST", "/api/v1/hotspots/refresh?market=cn"),
    ("GET", "/api/v1/hotspots/job-state"),
)
_TEST_PASSWORD = "isolated-hotspot-test-password"


@pytest.fixture
def main_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    data_dir = tmp_path / "data"
    static_dir = tmp_path / "static"
    env_file = tmp_path / "isolated.env"
    data_dir.mkdir()
    static_dir.mkdir()
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("STATIC_DIR", str(static_dir))
    monkeypatch.setenv("TICKFLOW_ENV_FILE", str(env_file))

    # Configure isolation before importing main: import creates a file logger
    # and restores auth state, even when application lifespan is not started.
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "static_dir", static_dir)
    monkeypatch.setattr(settings, "auth_password", "")

    import app.main as main_module
    from app.api import auth as auth_api
    from app.services import auth
    from app.services.hotspot.source import StubHotspotSource

    monkeypatch.setattr(auth, "_sessions", {})
    monkeypatch.setattr(auth, "_configured_cache", None)
    monkeypatch.setattr(auth_api, "_fail_counter", {})
    monkeypatch.setattr(
        main_module.app.state,
        "repo",
        SimpleNamespace(store=SimpleNamespace(data_dir=data_dir)),
        raising=False,
    )
    monkeypatch.setattr(
        main_module.app.state, "hotspot_cn_source", StubHotspotSource(), raising=False
    )
    # 港美已有本地行业聚合源, 这里注入 stub (supports=False) 才能测到
    # "source 不支持 → missing_mapping" 的 fail-closed 契约。
    for _market in ("hk", "us"):
        monkeypatch.setattr(
            main_module.app.state,
            f"hotspot_{_market}_source",
            StubHotspotSource(),
            raising=False,
        )

    @asynccontextmanager
    async def no_background_lifespan(_app: FastAPI):
        yield

    monkeypatch.setattr(main_module.app.router, "lifespan_context", no_background_lifespan)
    with TestClient(main_module.app, client=("127.0.0.1", 50000)) as client:
        try:
            yield client
        finally:
            # The real login persists its session internally. Revoke it before
            # retaining test artifacts; never log or export cookie/token values.
            client.post("/api/auth/logout")


@pytest.fixture
def authenticated_client(main_client: TestClient) -> TestClient:
    from app.services import auth

    auth.set_password(_TEST_PASSWORD)
    response = main_client.post("/api/auth/login", json={"password": _TEST_PASSWORD})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "authenticated": True}
    return main_client


def _assert_four_routes_serve_stub_data(client: TestClient) -> None:
    listing, detail, refresh, state = [
        client.request(method, path) for method, path in _HOTSPOT_REQUESTS
    ]
    assert [response.status_code for response in (listing, detail, refresh, state)] == [
        200, 200, 200, 200
    ]
    assert listing.json()["provider_used"] == "stub"
    assert listing.json()["hotspot_count"] == 5
    assert detail.json()["summary"]["topic"] == "人工智能"
    assert detail.json()["stock_count"] == len(detail.json()["stocks"]) == 4
    assert refresh.json()["status"] == "ok"
    assert state.json()["last_status"] == "success"
    assert state.json()["rows"] == 5


def test_main_hotspot_routes_allow_local_access_before_password_setup(
    main_client: TestClient,
) -> None:
    status = main_client.get("/api/auth/status")
    assert status.json() == {"configured": False, "authenticated": False}
    _assert_four_routes_serve_stub_data(main_client)


def test_main_password_protects_all_four_hotspot_routes(main_client: TestClient) -> None:
    from app.services import auth

    auth.set_password(_TEST_PASSWORD)
    for method, path in _HOTSPOT_REQUESTS:
        response = main_client.request(method, path)
        assert response.status_code == 401, (method, path, response.text)


def test_main_login_allows_all_four_hotspot_routes(
    authenticated_client: TestClient,
) -> None:
    _assert_four_routes_serve_stub_data(authenticated_client)


def test_main_authenticated_unknown_topic_is_404(
    authenticated_client: TestClient,
) -> None:
    response = authenticated_client.get("/api/v1/hotspots/QA未定义主题?market=cn")
    assert response.status_code == 404


@pytest.mark.parametrize("market", ["hk", "us"])
def test_main_authenticated_missing_mapping_contract(
    authenticated_client: TestClient, market: str,
) -> None:
    listing = authenticated_client.get("/api/v1/hotspots", params={"market": market})
    detail = authenticated_client.get(
        "/api/v1/hotspots/人工智能", params={"market": market}
    )
    refresh = authenticated_client.post(
        "/api/v1/hotspots/refresh", params={"market": market}
    )
    assert listing.status_code == detail.status_code == refresh.status_code == 200
    assert listing.json()["quality_status"] == "missing_mapping"
    assert listing.json()["hotspot_count"] == 0
    assert detail.json()["quality_status"] == "missing_mapping"
    assert detail.json()["stocks"] == []
    assert refresh.json()["status"] == "skipped"
