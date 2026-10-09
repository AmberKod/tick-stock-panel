"""us_deep API 测试 (九章融合 P1 配套) — mock provider, 不打真网。

覆盖: quote 正常返回过闸门、服务超时 available=false (HTTP 200)、
symbol 归一化 (HK 00700 → 0700.HK)、source_gate 对 openbb 放行/
仿名不放行、参数校验。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client():
    """隔离的 FastAPI app + TestClient (真实路由, mock provider 层)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.us_deep as us_deep

    app = FastAPI()
    app.include_router(us_deep.router)
    return TestClient(app), us_deep


def _quote_payload(symbol="NVDA"):
    """仿 OpenBB 真返回结构 (source=openbb, 原始盘口 dict)。"""
    return {
        "source": "openbb",
        "data": {
            "symbol": symbol,
            "last_price": 230.48,
            "bid": 230.35,
            "ask": 230.55,
            "high": 232.0,
            "low": 228.1,
            "ma_50d": 221.39,
            "ma_200d": 190.12,
            "year_high": 240.0,
            "year_low": 130.0,
        },
    }


# ───────────────────────── 正常返回过闸门 ─────────────────────────


class TestHappyPath:
    def test_quote_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "quote", lambda s, market="US": _quote_payload(s))
        r = tc.get("/api/us/deep/quote/NVDA")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["source"] == "openbb"
        assert body["symbol"] == "NVDA"
        assert body["market"] == "US"
        assert body["data"]["last_price"] == 230.48
        assert body["data"]["bid"] == 230.35
        assert body["data"]["ma_50d"] == 221.39

    def test_quote_hk_market_normalized(self, client, monkeypatch):
        """market=HK 时 provider 收到原始 symbol, 由 norm_symbol 归一 (单测另证)。"""
        tc, us_deep = client
        seen = {}

        def fake_quote(s, market="US"):
            seen["symbol"], seen["market"] = s, market
            return _quote_payload("0700.HK")

        monkeypatch.setattr(us_deep._provider, "quote", fake_quote)
        r = tc.get("/api/us/deep/quote/00700", params={"market": "HK"})
        body = r.json()
        assert body["available"] is True
        assert seen == {"symbol": "00700", "market": "HK"}

    def test_statement_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "statement",
            lambda s, kind="income", period="annual", limit=3: {
                "source": "openbb",
                "data": [{"fiscal_year": 2025, "period_ending": "2025-09-27",
                          "total_revenue": 416160000000.0}],
            },
        )
        r = tc.get("/api/us/deep/statement/AAPL", params={"kind": "income", "limit": 3})
        body = r.json()
        assert body["available"] is True
        assert body["source"] == "openbb"
        assert body["data"][0]["total_revenue"] == 416160000000.0

    def test_filings_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "filings",
            lambda s, limit=8, form_type=None: {
                "source": "openbb",
                "data": [{"form_type": "10-K", "filed_date": "2025-11-01",
                          "report_date": "2025-09-27", "cik": 320193}],
            },
        )
        r = tc.get("/api/us/deep/filings/AAPL")
        body = r.json()
        assert body["available"] is True
        assert body["data"][0]["form_type"] == "10-K"

    def test_summary_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "summary_text",
            lambda s, market="US": {"source": "openbb",
                                    "data": {"text": "NVDA 最新价 230.48"}},
        )
        r = tc.get("/api/us/deep/summary/NVDA")
        body = r.json()
        assert body["available"] is True
        assert "230.48" in body["data"]["text"]


# ───────────────────────── 服务超时 / 抛错 ─────────────────────────


class TestProviderError:
    def test_timeout_returns_unavailable_200(self, client, monkeypatch):
        """服务超时 → HTTP 200 + available=false + reason 含超时信息 (不占位)。"""
        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "quote",
            lambda s, market="US": (_ for _ in ()).throw(TimeoutError("URLError: timed out")),
        )
        r = tc.get("/api/us/deep/quote/NVDA")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["source"] == "openbb"
        assert "timed out" in body["reason"]

    def test_connection_error_returns_unavailable_200(self, client, monkeypatch):
        """服务没起 (URLError Connection refused) → 同样 available=false。"""
        import urllib.error

        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "quote",
            lambda s, market="US": (_ for _ in ()).throw(
                urllib.error.URLError("<urlopen error [Errno 111] Connection refused")),
        )
        body = tc.get("/api/us/deep/quote/NVDA").json()
        assert body["available"] is False
        assert "Connection refused" in body["reason"]

    def test_empty_data_returns_unavailable(self, client, monkeypatch):
        """OpenBB 返回空 results → no_data, 不占位。"""
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "quote", lambda s, market="US": {"source": "openbb", "data": {}})
        body = tc.get("/api/us/deep/quote/XXXX").json()
        assert body["available"] is False
        assert "no_data" in body["reason"]

    def test_no_placeholder_data_on_error(self, client, monkeypatch):
        """错误路径绝不带 data 字段伪装成功。"""
        tc, us_deep = client
        monkeypatch.setattr(
            us_deep._provider, "quote",
            lambda s, market="US": (_ for _ in ()).throw(TimeoutError("timed out")),
        )
        body = tc.get("/api/us/deep/quote/NVDA").json()
        assert "data" not in body


# ───────────────────────── 闸门拦截 ─────────────────────────


class TestGateBlocks:
    def test_fake_source_blocked(self, client, monkeypatch):
        """source 是 demo → 闸门拦下, available=false。"""
        tc, us_deep = client
        fake = _quote_payload()
        fake["source"] = "demo"
        monkeypatch.setattr(us_deep._provider, "quote", lambda s, market="US": fake)
        body = tc.get("/api/us/deep/quote/NVDA").json()
        assert body["available"] is False
        assert "source_gate_rejected" in body["reason"]

    def test_missing_source_blocked(self, client, monkeypatch):
        """payload 没有 source 字段 → 同样剔除 (fail-closed)。"""
        tc, us_deep = client
        no_source = _quote_payload()
        no_source.pop("source")
        monkeypatch.setattr(us_deep._provider, "quote", lambda s, market="US": no_source)
        body = tc.get("/api/us/deep/quote/NVDA").json()
        assert body["available"] is False
        assert "source_gate_rejected" in body["reason"]


# ───────────────────────── 参数校验 ─────────────────────────


class TestParamValidation:
    def test_bad_kind_rejected(self, client):
        body = client[0].get("/api/us/deep/statement/AAPL", params={"kind": "cashflow"}).json()
        assert body["available"] is False
        assert "bad_kind" in body["reason"]

    def test_bad_period_rejected(self, client):
        body = client[0].get("/api/us/deep/statement/AAPL", params={"period": "monthly"}).json()
        assert body["available"] is False
        assert "bad_period" in body["reason"]

    def test_bad_limit_rejected(self, client):
        body = client[0].get("/api/us/deep/statement/AAPL", params={"limit": 0}).json()
        assert body["available"] is False
        assert "bad_limit" in body["reason"]


# ───────────────────────── source_gate 白名单 ─────────────────────────


class TestSourceGateWhitelist:
    def test_openbb_whitelisted(self):
        from app.services.source_gate import REAL_SOURCES, is_real

        assert "openbb" in REAL_SOURCES
        assert is_real("openbb") is True

    def test_lookalike_not_whitelisted(self):
        from app.services.source_gate import is_real

        assert is_real("openbb_demo") is False
        assert is_real("openbb-fake") is False
        assert is_real("fake+openbb") is False  # 组合源任一部分不在白名单 → 拒


# ───────────────────────── provider 单元直测 ─────────────────────────


class TestProviderUnit:
    def test_norm_symbol_us_passthrough(self):
        from app.data_providers.jiuzhang_openbb_provider import norm_symbol

        assert norm_symbol("nvda") == "NVDA"
        assert norm_symbol("AAPL") == "AAPL"
        assert norm_symbol(" AAPL ") == "AAPL"
        assert norm_symbol("") == ""

    def test_norm_symbol_hk(self):
        from app.data_providers.jiuzhang_openbb_provider import norm_symbol

        # 九章 5 位港股代码 → yfinance 4 位 .HK
        assert norm_symbol("00700", "HK") == "0700.HK"
        assert norm_symbol("00700") == "0700.HK"          # 5 位纯数字自动识别
        assert norm_symbol("0700", "HK") == "0700.HK"
        assert norm_symbol("700", "HK") == "0700.HK"      # 短码左补零
        assert norm_symbol("00005") == "0005.HK"          # 汇丰
        assert norm_symbol("00700.HK", "HK") == "00700.HK"  # 已带后缀原样保留
        assert norm_symbol("3690", "HK") == "3690.HK"     # 美团 (4位无需补)

    def test_quote_returns_source_wrapper(self, monkeypatch):
        """quote 真实调用 _get 后包装 {source, data} (mock _get 不打网)。"""
        from app.data_providers import jiuzhang_openbb_provider as mod

        monkeypatch.setattr(mod, "_get", lambda path, **ps: [{"symbol": "NVDA", "last_price": 230.48}])
        out = mod.quote("NVDA")
        assert out["source"] == "openbb"
        assert out["data"]["symbol"] == "NVDA"
        assert out["data"]["last_price"] == 230.48

    def test_statement_returns_list_data(self, monkeypatch):
        from app.data_providers import jiuzhang_openbb_provider as mod

        rows = [{"fiscal_year": 2025}]
        monkeypatch.setattr(mod, "_get", lambda path, **ps: rows)
        out = mod.statement("AAPL", kind="income")
        assert out == {"source": "openbb", "data": rows}

    def test_summary_text_raises_on_quote_failure(self, monkeypatch):
        """fail-closed 适配: quote 抛错时 summary_text 上抛 (原版吞错)。"""
        from app.data_providers import jiuzhang_openbb_provider as mod

        def boom(code, market="US"):
            raise TimeoutError("URLError: timed out")

        monkeypatch.setattr(mod, "quote", boom)
        with pytest.raises(TimeoutError):
            mod.summary_text("NVDA")

    def test_summary_text_happy(self, monkeypatch):
        from app.data_providers import jiuzhang_openbb_provider as mod

        monkeypatch.setattr(
            mod, "quote",
            lambda code, market="US": {"source": "openbb", "data": {
                "symbol": "NVDA", "last_price": 230.48, "bid": 230.35, "ask": 230.55,
                "low": 228.1, "high": 232.0, "ma_50d": 221.39, "ma_200d": 190.12,
                "year_low": 130.0, "year_high": 240.0}},
        )
        out = mod.summary_text("NVDA")
        assert out["source"] == "openbb"
        assert "230.48" in out["data"]["text"]
        assert "221.39" in out["data"]["text"]

    def test_settings_drive_base_and_timeout(self, monkeypatch):
        """BASE/timeout 从 settings 读 (模块常量已消除)。"""
        from app.config import settings
        from app.data_providers import jiuzhang_openbb_provider as mod

        assert mod._base() == settings.openbb_base_url.rstrip("/")
        assert mod._timeout() == settings.openbb_timeout_s
        assert settings.openbb_base_url.startswith("http://host.docker.internal:6900")
