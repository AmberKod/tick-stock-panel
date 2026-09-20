"""热点工作区 API (FastAPI TestClient) 集成测试.

不启 uvicorn,用 httpx + FastAPI TestClient 走通路由,验证:
- /api/v1/hotspots GET/HTTP/contract
- /api/v1/hotspots/{topic}
- /api/v1/hotspots/refresh POST
- /api/v1/hotspots/job-state GET
- 港美市场 fail-closed (quality_status=missing_mapping)
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.hotspots import router as hotspots_router
from app.services.hotspot.models import HotspotResults
from app.services.hotspot.source import StubHotspotSource


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """构造最小 FastAPP,挂 hotspot router,注入 data_dir。"""

    app = FastAPI()
    # 通过一个最简单的 stub state 让 router 拿到 data_dir
    from types import SimpleNamespace

    class _RepoStub(SimpleNamespace):
        store = SimpleNamespace(data_dir=tmp_path / "data")

        def __init__(self, data_dir):
            super().__init__()
            self.store.data_dir = data_dir

    repo = _RepoStub(tmp_path / "data")
    app.state.repo = repo
    # 注入 cn source override(虽然 stub source 默认就行,但要测到 path)
    app.state.hotspot_cn_source = StubHotspotSource()
    # 港美注入 stub: 它 supports() 为 False, 用于验证"source 不支持 → missing_mapping"
    app.state.hotspot_hk_source = StubHotspotSource()
    app.state.hotspot_us_source = StubHotspotSource()

    app.include_router(hotspots_router)
    return TestClient(app)


@pytest.fixture
def client_hk_us(tmp_path: Path) -> TestClient:
    """注入港美行业聚合源 (fake instruments + fake 行情) 的 TestClient。"""
    from app.services.hotspot.hk_us_source import HkUsIndustryHotspotSource

    app = FastAPI()
    from types import SimpleNamespace

    class _RepoStub(SimpleNamespace):
        store = SimpleNamespace(data_dir=tmp_path / "data")

        def __init__(self, data_dir):
            super().__init__()
            self.store.data_dir = data_dir

    app.state.repo = _RepoStub(tmp_path / "data")

    def instruments(market):
        suffix = ".HK" if market == "hk" else ".US"
        rows = []
        # 两个行业: 科技 12 只 / 金融 12 只; 另有 3 只的小行业应被门槛过滤
        for industry, prefix in (("科技", "T"), ("金融", "F"), ("微型", "M")):
            count = 12 if industry != "微型" else 3
            for i in range(count):
                symbol = f"{prefix}{i:04d}{suffix}"
                rows.append({"symbol": symbol, "name": f"{industry}-{i}", "industry": industry})
        return rows

    def quotes(market, symbols):
        rows = []
        for symbol in symbols:
            # 科技全线上涨 +5%, 金融下跌 -2%, 微型 +50% (应被过滤)
            if symbol.startswith("T"):
                change = 0.05
            elif symbol.startswith("F"):
                change = -0.02
            else:
                change = 0.50
            rows.append({
                "symbol": symbol,
                "name": symbol,
                "change_pct": change,
                "amount": 1_000_000.0,
                "vol_ratio_5d": 1.2,
            })
        return rows, "daily", "2026-09-03"

    for market in ("hk", "us"):
        setattr(
            app.state,
            f"hotspot_{market}_source",
            HkUsIndustryHotspotSource(
                tmp_path / "data",
                instruments_loader=instruments,
                quote_loader=quotes,
            ),
        )

    app.include_router(hotspots_router)
    return TestClient(app)


def test_list_hotspots_cn_returns_envelope(client):
    response = client.get("/api/v1/hotspots", params={"market": "cn"})
    assert response.status_code == 200
    payload = response.json()
    # envelope 字段
    assert payload["enabled"] is True
    assert payload["market"] == "cn"
    assert "hotspots" in payload
    assert "hotspot_count" in payload
    assert payload["hotspot_count"] >= 1
    # 至少一个 topic 应有 heat_score 数值
    first = payload["hotspots"][0]
    assert 0 <= first["heat_score"] <= 100
    assert first["stage"]


def test_list_hotspots_invalid_market_returns_400(client):
    response = client.get("/api/v1/hotspots", params={"market": "xx"})
    assert response.status_code == 422 or response.status_code == 400


@pytest.mark.parametrize("market", ["hk", "us"])
def test_list_hotspots_hk_missing_mapping(client, market):
    response = client.get("/api/v1/hotspots", params={"market": market})
    assert response.status_code == 200
    payload = response.json()
    assert payload["hotspot_count"] == 0
    assert any(market in err for err in payload["source_errors"])
    assert payload["quality_status"] == "missing_mapping"


@pytest.mark.parametrize("market", ["hk", "us"])
def test_list_hotspots_hk_us_returns_industry_topics(client_hk_us, market):
    """港美走本地行业聚合: 行业即 topic, 小行业被门槛过滤。"""
    response = client_hk_us.get("/api/v1/hotspots", params={"market": market, "top": 10})
    assert response.status_code == 200
    payload = response.json()
    topics = [item["topic"] for item in payload["hotspots"]]
    assert "科技" in topics and "金融" in topics
    assert "微型" not in topics  # 成分 3 只 < MIN_MEMBERS(10)

    top = next(item for item in payload["hotspots"] if item["topic"] == "科技")
    assert top["rank"] == 1
    assert abs((top["change_pct"] or 0) - 0.05) < 1e-9  # 等权平均涨幅, 小数制
    assert top["snapshot_market"] == market
    assert top["topic_date"] == "2026-09-03"
    assert payload["provider"].startswith("hkus_industry")
    # 港美不可得字段必须显式声明, 不伪造
    assert "turnover_rate" in top["missing_fields"]
    assert "net_inflow" in top["missing_fields"]


def test_detail_hotspot_hk_us_returns_constituents(client_hk_us):
    """港美详情: 成分股可得 amount/量比(5日口径), 换手/净流为 None, 涨停恒 False。"""
    response = client_hk_us.get("/api/v1/hotspots/科技", params={"market": "hk"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["stock_count"] == 12
    assert len(payload["stocks"]) == 10  # top_stocks 默认 10
    stock = payload["stocks"][0]
    assert stock["is_limit_up"] is False  # 港美无涨跌停制度
    assert stock["turnover_rate"] is None
    assert stock["net_inflow"] is None
    assert abs((stock["volume_ratio"] or 0) - 1.2) < 1e-9  # 日K路取 vol_ratio_5d
    assert stock["code"].endswith(".HK")


def test_detail_hotspot_existing_topic(client):
    response = client.get("/api/v1/hotspots/人工智能", params={"market": "cn"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["topic"] == "人工智能"
    assert payload["stock_count"] >= 1
    assert payload["stocks"]
    # 龙头 stock 必有 hot_stock_score
    for stock in payload["leader_stocks"]:
        assert stock["hot_stock_score"] >= 0


def test_detail_hotspot_unknown_topic_returns_404(client):
    response = client.get("/api/v1/hotspots/不存在的题材", params={"market": "cn"})
    assert response.status_code == 404


def test_detail_hotspot_hk_returns_missing_mapping_envelope(client):
    response = client.get("/api/v1/hotspots/人工智能", params={"market": "hk"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["quality_status"] == "missing_mapping"
    assert payload["stock_count"] == 0


def test_refresh_endpoint_creates_state(client):
    response = client.post("/api/v1/hotspots/refresh", params={"market": "cn"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "empty"}
    assert payload["provider"]


def test_refresh_hk_returns_skipped(client):
    response = client.post("/api/v1/hotspots/refresh", params={"market": "hk"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "skipped"


def test_job_state_endpoint(client):
    response = client.get("/api/v1/hotspots/job-state")
    assert response.status_code == 200
    payload = response.json()
    # 初始 state 至少含 last_run / last_status
    assert "last_run" in payload or payload == {"last_run": None, "last_status": None, "rows": 0, "markets": {}}
    assert "rows" in payload


def test_list_hotspots_top_limit(client):
    response = client.get("/api/v1/hotspots", params={"market": "cn", "top": 2})
    payload = response.json()
    assert payload["hotspot_count"] <= 2


def test_list_hotspots_include_details_flag(client):
    # 强制 include_details 时返回 details dict(即便空 dict 占位也可)
    response = client.get("/api/v1/hotspots", params={"market": "cn", "include_details": "true"})
    assert response.status_code == 200
    payload = response.json()
    assert "details" in payload


class ThrowingSource(StubHotspotSource):
    name = "throwing_source"

    def discover(self, *, market="cn", top=20):
        raise TimeoutError("injected source timeout")


def test_source_exception_uses_complete_cache_with_consistent_quality(client):
    full = client.get("/api/v1/hotspots", params={"top": 20}).json()
    assert full["hotspot_count"] == 5
    small = client.get("/api/v1/hotspots", params={"top": 2}).json()
    assert small["hotspot_count"] == 2
    client.app.state.hotspot_cn_source = ThrowingSource()
    fallback = client.get("/api/v1/hotspots", params={"top": 20, "refresh": True})
    assert fallback.status_code == 200
    payload = fallback.json()
    assert payload["hotspot_count"] == 5
    assert payload["quality_status"] == "stale"
    assert payload["stale"] and payload["fallback_used"]
    assert payload["source_errors"]
    original_leaders = {row["topic"]: row["leader_stocks"] for row in full["hotspots"]}
    for row in payload["hotspots"]:
        assert row["stale"] and row["fallback_used"]
        assert row["quality_status"] == "stale"
        assert row["stale_age_hours"] == payload["stale_age_hours"]
        assert row["leader_stocks"] == original_leaders[row["topic"]]
    limited = client.get("/api/v1/hotspots", params={"top": 2}).json()
    assert limited["hotspot_count"] == 2


def test_source_exception_without_cache_returns_failed_empty_result(client):
    client.app.state.hotspot_cn_source = ThrowingSource()
    response = client.get("/api/v1/hotspots")
    assert response.status_code == 200
    payload = response.json()
    assert payload["hotspot_count"] == 0
    assert payload["quality_status"] == "failed"
    assert payload["source_errors"]


def test_degraded_source_response_preserves_quality_and_job_status(client):
    class DegradedSource(StubHotspotSource):
        def discover(self, *, market="cn", top=20):
            original = super().discover(market=market, top=top)
            return HotspotResults(
                list(original), provider_used=self.name, stale=True, fallback_used=True,
                stale_age_hours=48.0, source_errors=["upstream stale cache"], market=market,
            )

    client.app.state.hotspot_cn_source = DegradedSource()
    response = client.get("/api/v1/hotspots")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stale"] and payload["fallback_used"]
    assert payload["quality_status"] == "stale"
    assert payload["stale_age_hours"] == 48.0
    assert all(row["stale"] and row["fallback_used"] for row in payload["hotspots"])
    assert client.get("/api/v1/hotspots/job-state").json()["last_status"] == "degraded"
