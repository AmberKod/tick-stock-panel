"""批量个股新闻归因端点 (POST /api/news/batch-stock) 单测。

存在的理由 (PRD §3.2 面板 2「工程硬约束」): 异动归因要对上百条异动查新闻,
而搜索源带配额。逐条并发打 /api/news/stock = N 次外部调用直接打爆配额。
因此本端点必须保证: ① 内部串行限速; ② 结果按天缓存; ③ 单个 symbol 出错
不影响其他 symbol (部分可用)。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import news as news_api
from app.services import news_search


def _response(query: str, rows: list[dict], success: bool = True, error: str | None = None):
    """构造 SearchResponse (复用真实 dataclass, 走真实序列化路径)。"""
    return news_search.SearchResponse(
        query=query,
        results=[news_search.NewsItem(**row) for row in rows],
        provider="anspire",
        success=success,
        error_message=error,
    )


def _hit(url: str, title: str = "标题") -> dict:
    return {
        "title": title,
        "snippet": "摘要",
        "url": url,
        "source": news_search._extract_domain(url),
        "published_date": "2026-09-20",
    }


@pytest.fixture
def client():
    """隔离的 FastAPI app(只挂 news 路由, 避免主应用 lifespan 副作用)。"""
    app = FastAPI()
    app.include_router(news_api.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """清缓存 + 去掉限速等待 + 屏蔽本地维表读取, 保持测试快且无副作用。"""
    news_api.invalidate_batch_cache()
    monkeypatch.setattr(news_api, "_BATCH_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(news_api, "_lookup_name", lambda symbol: None)
    yield
    news_api.invalidate_batch_cache()


def test_partial_failure_does_not_affect_other_symbols(client, monkeypatch):
    """一个 symbol 搜索失败, 其余 symbol 照常返回 (fail-closed 但部分可用)。"""
    def fake_search(query, *, max_results=8, days=7, provider=None):
        if "BAD" in query:
            return _response(query, [], success=False, error="源不可用")
        return _response(query, [_hit("https://www.cninfo.com.cn/a/1")])

    monkeypatch.setattr(news_search, "search", fake_search)

    r = client.post("/api/news/batch-stock", json={"symbols": ["600519.SH", "BAD.SYMBOL", "000001.SZ"]})
    assert r.status_code == 200
    data = r.json()

    assert data["ok"] is True
    assert data["requested"] == 3
    assert data["error_count"] == 1
    assert data["results"]["BAD.SYMBOL"]["success"] is False
    assert data["results"]["BAD.SYMBOL"]["error"] == "源不可用"
    # 其他两个 symbol 不受影响
    assert data["results"]["600519.SH"]["success"] is True
    assert data["results"]["600519.SH"]["result_count"] == 1
    assert data["results"]["000001.SZ"]["success"] is True


def test_provider_exception_isolated_per_symbol(client, monkeypatch):
    """provider 抛异常时, 该 symbol 标记 error, 不掀整批。"""
    def fake_search(query, *, max_results=8, days=7, provider=None):
        if "BOOM" in query:
            raise RuntimeError("网络炸了")
        return _response(query, [_hit("https://finance.sina.com.cn/b/2")])

    monkeypatch.setattr(news_search, "search", fake_search)

    r = client.post("/api/news/batch-stock", json={"symbols": ["BOOM.SYMBOL", "600519.SH"]})
    assert r.status_code == 200
    data = r.json()
    assert "网络炸了" in (data["results"]["BOOM.SYMBOL"]["error"] or "")
    assert data["results"]["600519.SH"]["success"] is True


def test_hit_domains_deduped_not_normalized(client, monkeypatch):
    """返回**去重后的域名列表**, 不返回假装归一过的"独立来源数"。

    同一域名(去掉 www.)的多条只算一个; 不同域名分别保留。
    """
    def fake_search(query, *, max_results=8, days=7, provider=None):
        return _response(query, [
            _hit("https://www.cninfo.com.cn/a/1"),
            _hit("https://cninfo.com.cn/a/2"),          # 同域名(www 归一), 应合并
            _hit("https://finance.sina.com.cn/b/2"),
            _hit("https://www.cninfo.com.cn/a/3"),      # 重复, 应合并
        ])

    monkeypatch.setattr(news_search, "search", fake_search)

    r = client.post("/api/news/batch-stock", json={"symbols": ["600519.SH"]})
    entry = r.json()["results"]["600519.SH"]

    assert entry["hit_domains"] == ["cninfo.com.cn", "finance.sina.com.cn"]
    assert entry["hit_domain_count"] == 2
    assert entry["result_count"] == 4          # 条数不因域名去重而减少
    assert "未做出版方家族归一" in r.json()["domain_note"]


def test_cache_prevents_repeated_provider_calls(client, monkeypatch):
    """同一 symbol + 窗口 + 当天: 第二次不再打 provider (不重复消耗配额)。"""
    calls: list[str] = []

    def fake_search(query, *, max_results=8, days=7, provider=None):
        calls.append(query)
        return _response(query, [_hit("https://www.cninfo.com.cn/a/1")])

    monkeypatch.setattr(news_search, "search", fake_search)

    body = {"symbols": ["600519.SH", "000001.SZ"], "days": 3}
    first = client.post("/api/news/batch-stock", json=body).json()
    assert len(calls) == 2
    assert first["cached_count"] == 0

    second = client.post("/api/news/batch-stock", json=body).json()
    assert len(calls) == 2, "缓存命中不应再打 provider"
    assert second["cached_count"] == 2
    assert second["results"]["600519.SH"]["cached"] is True
    assert second["results"]["600519.SH"]["hit_domains"] == ["cninfo.com.cn"]

    # 换窗口 → 缓存键不同 → 重新查询
    client.post("/api/news/batch-stock", json={**body, "days": 7})
    assert len(calls) == 4


def test_failed_result_is_not_cached_for_an_hour(client, monkeypatch):
    """失败结果只能用短 TTL, 否则「重试」在 1 小时内拿不到恢复后的结果。

    场景: provider 挂了(配额耗尽/代理 502) → 用户补好 Key → 点重试。
    若失败也缓存 1 小时, 面板会一直卡在「不可用」且重试无效 —— 缓存的本意是
    "防止重试打爆配额", 不是"把失败钉死一小时"。
    """
    calls: list[str] = []

    def failing_search(query, *, max_results=8, days=7, provider=None):
        calls.append(query)
        return _response(query, [], success=False, error="余额不足或权限不足")

    monkeypatch.setattr(news_search, "search", failing_search)

    # ⚠️ 必须用**别的测试没用过的 symbol**: _batch_cache 是模块级全局, 跨测试保留。
    # 复用 600519.SH 会命中前面成功用例留下的缓存条目, 导致本用例莫名其妙失败。
    SYM = "300750.SZ"
    body = {"symbols": [SYM], "days": 3}

    # 第一次: 真打 provider, 失败
    first = client.post("/api/news/batch-stock", json=body).json()
    assert len(calls) == 1
    assert first["results"][SYM]["success"] is False
    assert first["error_count"] == 1

    # 紧接着第二次: 仍应命中缓存(短时间内不重复消耗配额 —— 防打爆的初衷要保住)
    second = client.post("/api/news/batch-stock", json=body).json()
    assert len(calls) == 1, "短 TTL 内失败也应命中缓存, 否则重试会打爆配额"
    assert second["results"][SYM]["cached"] is True

    # 常量钉子: 失败 TTL 必须显著短于成功 TTL(否则这条测试就失去意义)
    assert news_api._BATCH_CACHE_FAIL_TTL < news_api._BATCH_CACHE_TTL / 10

    # 推进一个**固定**的 120 秒(不跟着 FAIL_TTL 走, 否则测试会变成同义反复:
    # TTL 改成多大都能过期)。120 > 60(失败TTL) 且 120 < 3600(成功TTL)。
    ELAPSED = 120.0
    with news_api._batch_cache_lock:
        for key, (ts, entry) in list(news_api._batch_cache.items()):
            news_api._batch_cache[key] = (ts - ELAPSED, entry)

    # 第三次: 必须重新打 provider —— 这是「重试能恢复」的关键
    client.post("/api/news/batch-stock", json=body)
    assert len(calls) == 2, (
        f"失败缓存应在 {news_api._BATCH_CACHE_FAIL_TTL}s 后过期并重试 provider, "
        "否则面板永远卡在不可用"
    )


def test_duplicate_symbols_only_queried_once(client, monkeypatch):
    """同一批里重复 symbol 只查一次(去重保序)。"""
    calls: list[str] = []

    def fake_search(query, *, max_results=8, days=7, provider=None):
        calls.append(query)
        return _response(query, [])

    monkeypatch.setattr(news_search, "search", fake_search)

    data = client.post("/api/news/batch-stock", json={"symbols": ["600519.SH", "600519.SH"]}).json()
    assert len(calls) == 1
    assert data["requested"] == 2
    assert list(data["results"]) == ["600519.SH"]


def test_batch_cap_marks_overflow(client, monkeypatch):
    """超出单批上限的 symbol 明确标记未查询, 不静默丢弃。"""
    monkeypatch.setattr(news_api, "_BATCH_MAX_SYMBOLS", 2)
    monkeypatch.setattr(news_search, "search",
                        lambda query, **kw: _response(query, [_hit("https://a.com/1")]))

    data = client.post("/api/news/batch-stock", json={"symbols": ["A.SH", "B.SH", "C.SH"]}).json()
    assert data["results"]["C.SH"]["success"] is False
    assert "超出单批上限" in data["results"]["C.SH"]["error"]
    assert data["results"]["A.SH"]["success"] is True


def test_names_are_used_in_query(client, monkeypatch):
    """入参 names 用于构造查询词 (减少维表查询、提升召回)。"""
    seen: list[str] = []
    monkeypatch.setattr(news_search, "search",
                        lambda query, **kw: (seen.append(query), _response(query, []))[1])

    client.post("/api/news/batch-stock", json={
        "symbols": ["600519.SH"],
        "names": {"600519.SH": "贵州茅台"},
    })
    assert seen and "贵州茅台" in seen[0]
