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
                # ⚠️ 日期字段是 filing_date, 不是 filed_date —— 2026-10-16 实测
                # OpenBB /equity/fundamental/filings (provider=sec) 真实响应确认,
                # 旧 mock 写成 filed_date 与真实口径不符, 会误导前端取值。
                "data": [{"form_type": "10-K", "filing_date": "2025-11-01",
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


# ─────────────── 机构持股 / 内部人交易 / 分析师预期 ───────────────
# 2026-10-12 新增。mock provider, 不打真网。
# 守护四条:
#   1. 机构持股**必须走 nasdaq provider** —— OpenBB 的 /ownership/form_13f
#      只收 provider=sec, 本机 SEC 出口取不到数(实测空返回);
#   2. 源站 market_value 单位存疑(疑似千美元), **只以 _raw 透出不换算**,
#      不许任何换算后的市值流向前端;
#   3. 空列表 = 无覆盖 → no_data(不是空壳成功);
#   4. 分析师预期会出现"只有 symbol 的壳"(yfinance 无覆盖), 光判 not data
#      抓不到 → 核心值全 None 必须判 no_data。


def _institutional_payload():
    return {"source": "openbb", "data": [
        {"date": "2025-12-31", "owner": "Vanguard Group Inc",
         "shares_held": 1426283914.0, "shares_change": 26856752.0,
         "shares_change_pct": 0.01919, "market_value_raw": 480144217.0,
         "url": "https://example.invalid/v"}]}


def _insider_payload():
    return {"source": "openbb", "data": [
        {"transaction_date": "2026-10-06", "owner": "NEWSTEAD JENNIFER",
         "title": "Officer", "ownership_type": "Direct",
         "transaction_type": "Automatic Sell", "shares": 2399.0,
         "price": 332.01, "shares_held_after": 39593.0,
         "url": "https://example.invalid/i"}]}


def _estimates_payload(**over):
    base = {"symbol": "AAPL", "currency": "USD", "current_price": 336.64,
            "target_high": 405.0, "target_low": 215.0,
            "target_consensus": 328.09384, "target_median": 340.0,
            "recommendation": "buy", "recommendation_mean": 2.20455,
            "number_of_analysts": 39}
    base.update(over)
    return {"source": "openbb", "data": base}


class TestOwnershipAndEstimates:
    def test_institutional_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "institutional",
                            lambda s, limit=20: _institutional_payload())
        body = tc.get("/api/us/deep/institutional/AAPL?limit=5").json()
        assert body["available"] is True
        assert body["source"] == "openbb"
        assert body["data"][0]["owner"] == "Vanguard Group Inc"
        assert body["data"][0]["shares_change_pct"] == pytest.approx(0.01919)

    def test_institutional_bad_limit(self, client):
        tc, _ = client
        for q in ("limit=0", "limit=101"):
            body = tc.get(f"/api/us/deep/institutional/AAPL?{q}").json()
            assert body["available"] is False
            assert body["reason"].startswith("bad_limit")

    def test_institutional_empty_is_no_data(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "institutional",
                            lambda s, limit=20: {"source": "openbb", "data": []})
        body = tc.get("/api/us/deep/institutional/AAPL").json()
        assert body["available"] is False
        assert body["reason"].startswith("no_data")

    def test_insider_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "insider_trading",
                            lambda s, limit=20: _insider_payload())
        body = tc.get("/api/us/deep/insider/AAPL").json()
        assert body["available"] is True
        row = body["data"][0]
        assert row["owner"] == "NEWSTEAD JENNIFER"
        assert row["transaction_type"] == "Automatic Sell"
        assert row["shares"] == 2399.0
        assert row["price"] == 332.01

    def test_insider_bad_limit(self, client):
        tc, _ = client
        body = tc.get("/api/us/deep/insider/AAPL?limit=0").json()
        assert body["available"] is False
        assert body["reason"].startswith("bad_limit")

    def test_estimates_passes_gate(self, client, monkeypatch):
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "estimates", lambda s: _estimates_payload())
        body = tc.get("/api/us/deep/estimates/AAPL").json()
        assert body["available"] is True
        assert body["data"]["number_of_analysts"] == 39
        assert body["data"]["recommendation"] == "buy"
        assert body["data"]["target_median"] == 340.0

    def test_estimates_shell_without_values_is_no_data(self, client, monkeypatch):
        """yfinance 无覆盖时返回只有 symbol 的壳 —— 不许冒充成功。"""
        tc, us_deep = client
        monkeypatch.setattr(us_deep._provider, "estimates", lambda s: _estimates_payload(
            currency=None, current_price=None, target_high=None, target_low=None,
            target_consensus=None, target_median=None, recommendation=None,
            recommendation_mean=None, number_of_analysts=None))
        body = tc.get("/api/us/deep/estimates/ZZZZ").json()
        assert body["available"] is False
        assert body["reason"].startswith("no_data")

    def test_provider_down_is_available_false(self, client, monkeypatch):
        """OpenBB 没起时三个端点都必须 fail-closed, 不能崩也不能造假。"""
        tc, us_deep = client

        def _boom(*a, **k):
            raise RuntimeError("<urlopen error [Errno 101] Network is unreachable>")

        monkeypatch.setattr(us_deep._provider, "institutional", _boom)
        monkeypatch.setattr(us_deep._provider, "insider_trading", _boom)
        monkeypatch.setattr(us_deep._provider, "estimates", _boom)
        for path in ("institutional/AAPL", "insider/AAPL", "estimates/AAPL"):
            body = tc.get(f"/api/us/deep/{path}").json()
            assert body["available"] is False
            assert body["reason"].startswith("provider_error")


class TestProviderFieldMapping:
    """provider 层字段映射 —— 锁住"存疑单位只透 raw"与"走 nasdaq 不走 sec"。"""

    def test_institutional_renames_and_keeps_raw_market_value(self, monkeypatch):
        from app.data_providers import jiuzhang_openbb_provider as prov

        seen = {}

        def _fake(path, **kw):
            seen.update(kw)
            seen["path"] = path
            return [{"symbol": "AAPL", "date": "2025-12-31", "owner_name": "Vanguard Group Inc",
                     "shares_held": 1426283914.0, "shares_change": 26856752.0,
                     "shares_change_percent": 0.01919, "market_value": 480144217.0,
                     "url": "https://example.invalid/v"}]

        monkeypatch.setattr(prov, "_get", _fake)
        row = prov.institutional("AAPL", limit=5)["data"][0]
        assert row["owner"] == "Vanguard Group Inc"
        assert row["date"] == "2025-12-31"
        # 存疑单位: 只透 raw, 不产出换算后的市值字段
        assert row["market_value_raw"] == 480144217.0
        assert "market_value" not in row

    def test_institutional_uses_nasdaq_not_sec(self, monkeypatch):
        """/ownership/form_13f 只收 provider=sec 且本机取不到数(实测空返回)。"""
        from app.data_providers import jiuzhang_openbb_provider as prov

        seen = {}

        def _fake(path, **kw):
            seen.update(kw)
            seen["path"] = path
            return []

        monkeypatch.setattr(prov, "_get", _fake)
        prov.institutional("AAPL")
        assert seen["path"] == "/equity/ownership/institutional"
        assert seen["provider"] == "nasdaq"

    def test_insider_maps_transaction_fields(self, monkeypatch):
        from app.data_providers import jiuzhang_openbb_provider as prov

        monkeypatch.setattr(prov, "_get", lambda path, **kw: [
            {"symbol": "AAPL", "transaction_date": "2026-10-06", "owner_name": "NEWSTEAD JENNIFER",
             "owner_title": "Officer", "ownership_type": "Direct",
             "transaction_type": "Automatic Sell", "securities_transacted": 2399.0,
             "transaction_price": 332.01, "shares_held": 39593.0,
             "url": "https://example.invalid/i"}])
        row = prov.insider_trading("AAPL", limit=5)["data"][0]
        assert row["owner"] == "NEWSTEAD JENNIFER"
        assert row["title"] == "Officer"
        assert row["shares"] == 2399.0
        assert row["price"] == 332.01
        assert row["shares_held_after"] == 39593.0

    def test_estimates_maps_consensus_fields(self, monkeypatch):
        from app.data_providers import jiuzhang_openbb_provider as prov

        monkeypatch.setattr(prov, "_get", lambda path, **kw: [
            {"symbol": "AAPL", "currency": "USD", "current_price": 336.64,
             "target_high": 405.0, "target_low": 215.0, "target_consensus": 328.09384,
             "target_median": 340.0, "recommendation": "buy",
             "recommendation_mean": 2.20455, "number_of_analysts": 39}])
        d = prov.estimates("AAPL")["data"]
        assert d["number_of_analysts"] == 39
        assert d["recommendation"] == "buy"
        assert d["target_consensus"] == pytest.approx(328.09384)
        assert d["currency"] == "USD"
