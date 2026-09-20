"""新闻搜索服务单测。

重点锁定 fail-closed: 没配 Key / 源报错时返回明确的 error,
而不是空列表(空列表会让前端用户以为"今天没新闻")。
"""
from __future__ import annotations

import pytest

from app.services import news_search


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _ok_payload() -> dict:
    return {
        "code": 200,
        "results": [
            {"title": "敦煌种业涨停", "content": "x" * 600, "url": "https://www.cninfo.com.cn/a/1",
             "date": "2026-09-17"},
            {"title": "种业板块走强", "content": "短摘要", "url": "https://finance.sina.com.cn/b/2",
             "date": "2026-09-17"},
        ],
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    news_search.invalidate_cache()
    yield
    news_search.invalidate_cache()


# ── fail-closed ────────────────────────────────────────────────


def test_search_fails_closed_without_key(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: [])
    r = news_search.search("贵州茅台")
    assert r.success is False
    assert "未配置 Key" in (r.error_message or "")
    assert r.results == []


def test_empty_query_rejected():
    r = news_search.search("   ")
    assert r.success is False
    assert "query 不能为空" in (r.error_message or "")


def test_provider_error_surfaces_message(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", lambda *a, **kw: _FakeResponse(401))
    r = news_search.search("测试")
    assert r.success is False
    assert "API Key 无效" in (r.error_message or "")


def test_balance_error_message(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", lambda *a, **kw: _FakeResponse(403))
    r = news_search.search("测试")
    assert "余额不足" in (r.error_message or "")


def test_api_error_code_surfaces(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", lambda *a, **kw: _FakeResponse(200, {"code": 500, "msg": "内部错误"}))
    r = news_search.search("测试")
    assert r.success is False
    assert "内部错误" in (r.error_message or "")


# ── 正常解析 ────────────────────────────────────────────────────


def test_parses_results_and_truncates_snippet(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", lambda *a, **kw: _FakeResponse(200, _ok_payload()))

    r = news_search.search("转基因 板块", max_results=5)
    assert r.success is True
    assert r.provider == "anspire"
    assert len(r.results) == 2
    first = r.results[0]
    assert first.title == "敦煌种业涨停"
    assert first.snippet.endswith("...") and len(first.snippet) == 503
    assert first.source == "cninfo.com.cn"   # 域名去掉 www.
    assert first.published_date == "2026-09-17"


def test_max_results_capped_at_50(monkeypatch):
    captured = {}

    def _fake_get(url, **kw):
        captured.update(kw.get("params") or {})
        return _FakeResponse(200, {"code": 200, "results": []})

    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", _fake_get)
    news_search.search("x", max_results=999)
    assert captured["top_k"] == 50


# ── 缓存 ───────────────────────────────────────────────────────


def test_cache_reuses_response(monkeypatch):
    calls = []

    def _fake_get(url, **kw):
        calls.append(url)
        return _FakeResponse(200, _ok_payload())

    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", _fake_get)

    news_search.search("缓存测试")
    news_search.search("缓存测试")
    assert len(calls) == 1

    news_search.invalidate_cache()
    news_search.search("缓存测试")
    assert len(calls) == 2


def test_failed_responses_are_not_cached(monkeypatch):
    calls = []

    def _fake_get(url, **kw):
        calls.append(url)
        return _FakeResponse(401)

    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1"])
    monkeypatch.setattr(news_search.httpx, "get", _fake_get)

    news_search.search("失败测试")
    news_search.search("失败测试")
    assert len(calls) == 2   # 失败不该被缓存, 否则配好 Key 后要等 TTL


# ── 多 Key 轮询 ────────────────────────────────────────────────


def test_multi_key_round_robin(monkeypatch):
    monkeypatch.setattr(news_search, "_load_keys", lambda name: ["k1", "k2"])
    seen = []

    def _fake_get(url, **kw):
        seen.append(kw["headers"]["Authorization"])
        return _FakeResponse(200, {"code": 200, "results": []})

    monkeypatch.setattr(news_search.httpx, "get", _fake_get)
    # 注意: 中间不能调 invalidate_cache — 它会重建 provider, 轮询从头开始。
    # 这正是"连续请求应轮换 Key"的真实用法。
    news_search.search("a", max_results=1)
    news_search.search("b", max_results=1)
    assert seen == ["Bearer k1", "Bearer k2"]


def test_split_keys_supports_comma_and_newline():
    assert news_search._split_keys("a,b\n c ,, ") == ["a", "b", "c"]
    assert news_search._split_keys(None) == []


# ── 查询构造 ────────────────────────────────────────────────────


def test_stock_query_cn_vs_foreign():
    q = news_search.stock_query("600354.SH", "敦煌种业")
    assert "敦煌种业" in q and "600354.SH" in q and "公告" in q

    q_en = news_search.stock_query("00700.HK", "腾讯控股")
    assert "stock news" in q_en


def test_concept_query():
    assert "转基因" in news_search.concept_query("转基因")
    assert news_search.concept_query("") == ""


def test_extract_domain_handles_bad_url():
    assert news_search._extract_domain("not-a-url") == "未知来源"
    assert news_search._extract_domain("") == "未知来源"
