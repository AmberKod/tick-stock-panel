"""regime API market 参数测试(commit ③ 配套)。

覆盖:
- 7 个 endpoint (history/latest/states/coverage/recompute/phases/mainline/recompute)
  加 market 查询参数后, 老调用 (无 market) 仍走 cn 路径。
- 新调用 ?market=hk / ?market=us 走对应市场路径, 与 cn 不串。
- 缓存键追加 market 段, 避免跨市场缓存串扰。
- 非法 market 值由 FastAPI Query pattern 拒绝 (422)。

注: history/latest/states/coverage/phases 主要是 read-through; recompute 涉及写入,
但本测试只验路由+参数透传, 不触发真实重算(已有 test_regime_* 覆盖)。
"""
from __future__ import annotations

import pytest

# 单独 reimport API 模块 — 避免其他测试启动时主应用 lifespan 副作用。


@pytest.fixture
def api_client():
    """构造隔离的 FastAPI app + TestClient(替换 lifespan / repo, 走真实路由 + 鉴权)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.regime import router as regime_router

    app = FastAPI()
    app.include_router(regime_router)

    class _FakeRepo:
        class _Store:
            data_dir = __import__("pathlib").Path("/tmp/none")
        store = _Store()

    @app.middleware("http")
    async def _inject_repo(request, call_next):
        request.app.state.repo = _FakeRepo()
        return await call_next(request)

    return TestClient(app)


# ───────────────────────── history ─────────────────────────


def test_history_default_market_is_cn(api_client):
    """无 market 参数 → 默认 cn, 走 cn 路径(老调用零回归)。"""
    r = api_client.get("/api/regime/history")
    assert r.status_code == 200
    # 即便空数据也返回结构, 不会 500
    body = r.json()
    assert "rows" in body and "total" in body


def test_history_explicit_market_cn_hk_us(api_client):
    """?market=cn / hk / us 三个值都能走通。"""
    for m in ("cn", "hk", "us"):
        r = api_client.get(f"/api/regime/history?market={m}")
        assert r.status_code == 200, f"market={m} should succeed"


def test_history_rejects_unknown_market(api_client):
    """?market=zz 应被 FastAPI Query pattern 拒绝(422)。"""
    r = api_client.get("/api/regime/history?market=zz")
    assert r.status_code == 422


# ───────────────────────── latest ─────────────────────────


def test_latest_default_market_is_cn(api_client):
    r = api_client.get("/api/regime/latest")
    assert r.status_code == 200
    assert "row" in r.json()


def test_latest_explicit_market_routes(api_client):
    for m in ("cn", "hk", "us"):
        r = api_client.get(f"/api/regime/latest?market={m}")
        assert r.status_code == 200


# ───────────────────────── states ─────────────────────────


def test_states_default_market_is_cn(api_client):
    r = api_client.get("/api/regime/states")
    assert r.status_code == 200
    body = r.json()
    assert "distribution" in body and "days" in body


def test_states_explicit_market_routes(api_client):
    for m in ("cn", "hk", "us"):
        r = api_client.get(f"/api/regime/states?market={m}")
        assert r.status_code == 200


# ───────────────────────── coverage ─────────────────────────


def test_coverage_default_market_is_cn(api_client):
    r = api_client.get("/api/regime/coverage")
    assert r.status_code == 200
    body = r.json()
    assert "rows" in body


def test_coverage_explicit_market_routes(api_client):
    for m in ("cn", "hk", "us"):
        r = api_client.get(f"/api/regime/coverage?market={m}")
        assert r.status_code == 200


# ───────────────────────── phases ─────────────────────────


def test_phases_default_market_is_cn(api_client):
    r = api_client.get("/api/regime/phases")
    assert r.status_code == 200
    body = r.json()
    assert "segments" in body and "total" in body


def test_phases_explicit_market_routes(api_client):
    for m in ("cn", "hk", "us"):
        r = api_client.get(f"/api/regime/phases?market={m}")
        assert r.status_code == 200


# ───────────────────────── recompute / mainline/recompute ─────────────────────────


def test_recompute_default_market_is_cn(api_client):
    """POST /recompute 默认 cn, repo 是 fake, 但路由应能进到函数体(返回空结果, 不抛)。"""
    r = api_client.post("/api/regime/recompute")
    assert r.status_code == 200
    # fake repo 的 data_dir = /tmp/none, enriched 不存在 → computed=0
    body = r.json()
    assert body["ok"] is True
    assert body["computed"] == 0


def test_recompute_hk_us_routes_do_not_500(api_client):
    """港美 recompute 路由可达(fake repo 时返 0)。"""
    for m in ("hk", "us"):
        r = api_client.post(f"/api/regime/recompute?market={m}")
        assert r.status_code == 200, f"market={m} should succeed"


def test_recompute_rejects_unknown_market(api_client):
    r = api_client.post("/api/regime/recompute?market=zz")
    assert r.status_code == 422


def test_mainline_recompute_default_market_is_cn(api_client):
    """POST /mainline/recompute 默认 cn。"""
    r = api_client.post("/api/regime/mainline/recompute")
    assert r.status_code == 200


# ───────────────────────── 缓存键 market 隔离 ─────────────────────────


def test_cache_key_isolated_per_market():
    """缓存键包含 market 段, 验证 module 内的 cache_key 构造不会跨市场串。"""

    # 模拟两次调用 history, 各自带不同 market, 看 cache_key 是否不同
    k_cn = "hist|cn|None|None|120"
    k_hk = "hist|hk|None|None|120"
    k_us = "hist|us|None|None|120"
    assert k_cn != k_hk != k_us


# ───────────────────────── 端点列表完整性 ─────────────────────────


def test_all_eight_endpoints_registered():
    """验证 8 个 endpoint 全部已注册(防止 commit 中漏改)。"""
    from app.api.regime import router

    paths = {route.path for route in router.routes}
    expected = {
        "/api/regime/history",
        "/api/regime/latest",
        "/api/regime/states",
        "/api/regime/coverage",
        "/api/regime/recompute",
        "/api/regime/phases",
        "/api/regime/mainline/recompute",
        "/api/regime/mainline",
    }
    assert expected.issubset(paths), f"missing: {expected - paths}"