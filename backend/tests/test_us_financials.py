"""us_financials API 测试 (九章融合 P0-2 配套) — mock provider, 不打真网。

覆盖: 正常返回过闸门、provider 抛错 available=false (HTTP 200)、
source 不是 sec-edgar 时被闸门拦下。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client():
    """隔离的 FastAPI app + TestClient (真实路由, mock provider 层)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.us_financials as us_fin

    app = FastAPI()
    app.include_router(us_fin.router)
    return TestClient(app), us_fin


def _real_payload(symbol="AAPL"):
    """仿 SEC 真返回结构 (source=sec-edgar, 原始美元值)。"""
    return {
        "ticker": symbol,
        "cik": 320193,
        "source": "sec-edgar",
        "count": 2,
        "metrics": {
            "revenue": {
                "label_cn": "营业收入",
                "unit": "USD",
                "latest": 10942000000.0,
                "quarterly": [{"start": "2026-03-29", "end": "2026-06-27",
                               "fp": "Q2", "val": 10942000000.0, "fy": 2026, "form": "10-Q"}],
                "annual": [{"end": "2025-09-27", "val": 40000000000.0, "fy": 2025}],
            },
            "net_income": {
                "label_cn": "净利润",
                "unit": "USD",
                "latest": 2979000000.0,
                "quarterly": [{"start": "2026-03-29", "end": "2026-06-27",
                               "fp": "Q2", "val": 2979000000.0, "fy": 2026, "form": "10-Q"}],
                "annual": [],
            },
        },
    }


# ───────────────────────── 正常返回过闸门 ─────────────────────────


class TestHappyPath:
    def test_real_data_passes_gate(self, client, monkeypatch):
        tc, us_fin = client
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: _real_payload(s))
        r = tc.get("/api/us/financials/AAPL")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["source"] == "sec-edgar"
        assert body["symbol"] == "AAPL"
        assert body["cik"] == 320193
        assert body["count"] == 2
        assert body["metrics"]["revenue"]["latest"] == 10942000000.0
        assert body["metrics"]["net_income"]["latest"] == 2979000000.0

    def test_none_returns_unavailable(self, client, monkeypatch):
        """provider 返回 None (非美股/无数据) → available=false, HTTP 200。"""
        tc, us_fin = client
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: None)
        r = tc.get("/api/us/financials/XXXX")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["source"] == "sec-edgar"
        assert "no_data" in body["reason"]


# ───────────────────────── provider 抛错 ─────────────────────────


class TestProviderError:
    def test_exception_returns_unavailable_200(self, client, monkeypatch):
        """provider 超时/抛错 → HTTP 200 + available=false + 具体原因 (不占位)。"""
        tc, us_fin = client
        monkeypatch.setattr(
            us_fin._provider, "fundamentals",
            lambda s: (_ for _ in ()).throw(TimeoutError("URLError: timed out")),
        )
        r = tc.get("/api/us/financials/AAPL")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["source"] == "sec-edgar"
        assert "timed out" in body["reason"]

    def test_no_placeholder_values_on_error(self, client, monkeypatch):
        """错误路径绝不带占位 metrics/cik 字段伪装成功。"""
        tc, us_fin = client
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: None)
        body = tc.get("/api/us/financials/AAPL").json()
        assert "metrics" not in body
        assert "cik" not in body
        assert "count" not in body


# ───────────────────────── 闸门拦截 ─────────────────────────


class TestGateBlocks:
    def test_fake_source_blocked(self, client, monkeypatch):
        """source 不是 sec-edgar (如 demo) → 闸门拦下, available=false。"""
        tc, us_fin = client
        fake = _real_payload()
        fake["source"] = "demo"
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: fake)
        r = tc.get("/api/us/financials/AAPL")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert "source_gate_rejected" in body["reason"]

    def test_missing_source_blocked(self, client, monkeypatch):
        """record 里根本没有 source 字段 → 同样剔除 (fail-closed)。"""
        tc, us_fin = client
        no_source = _real_payload()
        no_source.pop("source")
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: no_source)
        body = tc.get("/api/us/financials/AAPL").json()
        assert body["available"] is False
        assert "source_gate_rejected" in body["reason"]

    def test_empty_metrics_dict_is_not_data(self, client, monkeypatch):
        """空 metrics 的 dict 视为 no_data (provider 侧已保证 None, 防御)。"""
        tc, us_fin = client
        monkeypatch.setattr(us_fin._provider, "fundamentals", lambda s: None)
        body = tc.get("/api/us/financials/AAPL").json()
        assert body["available"] is False


# ───────────────────────── provider 单元直测 ─────────────────────────


class TestProviderUnit:
    def test_provider_delegates_to_module_function(self, monkeypatch, tmp_path):
        from app.data_providers import jiuzhang_us_fund_provider as mod

        monkeypatch.setattr(mod, "fundamentals", lambda t, metrics=None: _real_payload(t))
        try:
            p = mod.JiuzhangUSFundProvider(cache_dir=tmp_path)
            d = p.fundamentals("AAPL")
        finally:
            mod.set_cache_dir(None)  # 恢复默认, 避免污染其他测试
        assert d["source"] == "sec-edgar"
        assert d["ticker"] == "AAPL"

    def test_set_cache_dir_restores_default(self, tmp_path):
        from app.config import settings
        from app.data_providers import jiuzhang_us_fund_provider as mod

        original = mod.CACHE_DIR
        try:
            mod.set_cache_dir(tmp_path)
            assert mod.CACHE_DIR == tmp_path
        finally:
            mod.set_cache_dir(None)
        assert mod.CACHE_DIR == settings.data_dir / "us_fund_cache"
        assert original == mod.CACHE_DIR
