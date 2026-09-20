"""新闻/舆情搜索服务 — 给热点工作区补"为什么涨"的那一维。

热点工作区原本只有"概念热度"(本地行情聚合), 回答不了"今天发生了什么"。
本模块接外部搜索源补新闻/公告/事件。

设计取舍 (对齐参考项目 daily_stock_analysis 的 search_service):
  - provider 抽象 + 统一 SearchResponse, 便于后续加源
  - 多 Key 轮询 (cycle) — 单 Key 配额打满时自动换下一个
  - 结果 TTL 缓存 — 热点页会反复点同一只股/同一个概念
  - **fail-closed**: 源不可达/未配 Key 时返回空 + 明确 error,
    绝不拿缓存过期数据或编造内容伪装成实时新闻

密钥来自 secrets_store (设置页配置, DPAPI 加密), 不读环境变量、不落 preferences。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import cycle
from typing import Any
from urllib.parse import urlparse

import httpx

from app import secrets_store

logger = logging.getLogger(__name__)

_CACHE_TTL = 300.0
_HTTP_TIMEOUT = 10.0

_cache: dict[tuple, tuple[float, SearchResponse]] = {}
_cache_lock = threading.RLock()


@dataclass
class NewsItem:
    title: str
    snippet: str
    url: str
    source: str
    published_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "snippet": self.snippet,
            "url": self.url,
            "source": self.source,
            "published_date": self.published_date,
        }


@dataclass
class SearchResponse:
    query: str
    results: list[NewsItem] = field(default_factory=list)
    provider: str = ""
    success: bool = True
    error_message: str | None = None
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [r.to_dict() for r in self.results],
            "result_count": len(self.results),
            "provider": self.provider,
            "success": self.success,
            "error_message": self.error_message,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def _extract_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.replace("www.", "")
        return host or "未知来源"
    except Exception:
        return "未知来源"


# ── Provider ──────────────────────────────────────────────────


class BaseSearchProvider:
    """搜索引擎基类: 多 Key 轮询 + 统一响应。"""

    name = "abstract"
    secret_name = ""

    def __init__(self, api_keys: list[str]) -> None:
        self._api_keys = [k for k in api_keys if k]
        self._key_cycle = cycle(self._api_keys) if self._api_keys else None

    @property
    def configured(self) -> bool:
        return bool(self._api_keys)

    @property
    def key_fingerprint(self) -> tuple[str, ...]:
        """用于判断缓存的 provider 是否还匹配当前 Key 列表(改 Key 后重建)。"""
        return tuple(self._api_keys)

    def search(self, query: str, max_results: int = 8, days: int = 7) -> SearchResponse:
        raise NotImplementedError

    def _next_key(self) -> str | None:
        return next(self._key_cycle) if self._key_cycle else None


class AnspireSearchProvider(BaseSearchProvider):
    """Anspire AI Search — 国内源, A股/港股/美股新闻舆情检索。

    协议 (参考 daily_stock_analysis):
      GET https://plugin.anspire.cn/api/ntsearch/search
      Header: Authorization: Bearer <key>
      Params: query / top_k(<=50) / FromTime / ToTime / region_mode
      响应: {code: 200, results: [{title, content, url, date}]}
    """

    name = "anspire"
    secret_name = "anspire_api_key"
    _URL = "https://plugin.anspire.cn/api/ntsearch/search"

    def search(self, query: str, max_results: int = 8, days: int = 7) -> SearchResponse:
        started = time.perf_counter()
        if not self.configured:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="未配置 Anspire API Key")
        key = self._next_key()
        if not key:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="无可用 Key")

        now = datetime.now()
        params = {
            "query": query,
            "top_k": max(1, min(int(max_results), 50)),
            "FromTime": (now - timedelta(days=max(1, days))).strftime("%Y-%m-%d %H:%M:%S"),
            "ToTime": now.strftime("%Y-%m-%d %H:%M:%S"),
            "region_mode": 2,
        }
        try:
            resp = httpx.get(
                self._URL,
                params=params,
                headers={"Authorization": f"Bearer {key}"},
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.TimeoutException:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="请求超时", elapsed_s=time.perf_counter() - started)
        except Exception as e:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message=f"网络请求失败: {e}",
                                  elapsed_s=time.perf_counter() - started)

        if resp.status_code == 401:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="API Key 无效", elapsed_s=time.perf_counter() - started)
        if resp.status_code == 403:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="余额不足或权限不足",
                                  elapsed_s=time.perf_counter() - started)
        if resp.status_code != 200:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message=f"HTTP {resp.status_code}",
                                  elapsed_s=time.perf_counter() - started)

        try:
            data = resp.json()
        except ValueError:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message="响应非 JSON", elapsed_s=time.perf_counter() - started)

        code = data.get("code")
        if code is not None and code != 200:
            return SearchResponse(query, provider=self.name, success=False,
                                  error_message=str(data.get("msg") or f"API 错误码 {code}"),
                                  elapsed_s=time.perf_counter() - started)

        items: list[NewsItem] = []
        for row in (data.get("results") or [])[:max_results]:
            snippet = row.get("content") or ""
            if isinstance(snippet, str) and len(snippet) > 500:
                snippet = snippet[:500] + "..."
            url = row.get("url") or ""
            items.append(
                NewsItem(
                    title=row.get("title") or "",
                    snippet=snippet,
                    url=url,
                    source=_extract_domain(url),
                    published_date=row.get("date") or None,
                )
            )
        return SearchResponse(query, results=items, provider=self.name, success=True,
                              elapsed_s=time.perf_counter() - started)


# 后续接入其他源时在此登记即可 (Tavily / 博查 / Brave / SerpAPI / SearXNG)
PROVIDER_CLASSES: dict[str, type[BaseSearchProvider]] = {
    AnspireSearchProvider.name: AnspireSearchProvider,
}

# 降级顺序: 前面的源失败就换下一个
PROVIDER_FALLBACK_ORDER: tuple[str, ...] = ("anspire",)


# ── 服务编排 ────────────────────────────────────────────────────


def _split_keys(raw: str | None) -> list[str]:
    """支持逗号/换行分隔的多 Key (负载均衡 + 配额打满时轮换)。"""
    if not raw:
        return []
    parts = str(raw).replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def _load_keys(secret_name: str) -> list[str]:
    """取密钥: 设置页写入的 secrets.json 优先, 否则环境变量。

    复用既有 app.secrets_store (与 TickFlow / AI Key 同一套存储,
    ENCRYPTED_FIELDS 里的字段落盘经 DPAPI 加密)。
    """
    env_name = secret_name.upper()
    return _split_keys(secrets_store.get_env_backed_secret(secret_name, env_name))


# provider 必须按 name 复用: 每次请求新建会让多 Key 的 cycle 从头开始,
# 结果所有请求都砸在第一个 Key 上, 轮询形同虚设。
_provider_cache: dict[str, BaseSearchProvider] = {}


def _build_provider(name: str) -> BaseSearchProvider | None:
    cls = PROVIDER_CLASSES.get(name)
    if cls is None:
        return None
    keys = _load_keys(cls.secret_name)
    if not keys:
        _provider_cache.pop(name, None)
        return None
    cached = _provider_cache.get(name)
    if cached is not None and cached.key_fingerprint == tuple(keys):
        return cached
    provider = cls(keys)
    _provider_cache[name] = provider
    return provider


def invalidate_cache() -> None:
    """清结果缓存 + provider 实例(改 Key 后必须重建, 否则还在用旧 Key)。"""
    with _cache_lock:
        _cache.clear()
    _provider_cache.clear()


def search(
    query: str,
    *,
    max_results: int = 8,
    days: int = 7,
    provider: str | None = None,
) -> SearchResponse:
    """统一搜索入口: 按降级顺序尝试各源, 全部失败则返回最后一个错误。"""
    query = (query or "").strip()
    if not query:
        return SearchResponse(query="", success=False, error_message="query 不能为空")

    key = (query, max_results, days, provider)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            return hit[1]

    order = [provider] if provider else list(PROVIDER_FALLBACK_ORDER)
    last: SearchResponse | None = None
    errors: list[str] = []

    for name in order:
        src = _build_provider(name)
        if src is None:
            errors.append(f"{name}: 未配置 Key")
            continue
        resp = src.search(query, max_results=max_results, days=days)
        if resp.success and resp.results:
            with _cache_lock:
                _cache[key] = (time.monotonic(), resp)
            return resp
        last = resp
        if resp.error_message:
            errors.append(f"{name}: {resp.error_message}")

    failure = last or SearchResponse(query=query, success=False)
    failure.results = []
    failure.success = False
    failure.error_message = "; ".join(errors) or "无可用搜索源"
    return failure


# ── 查询构造 ────────────────────────────────────────────────────

_CN_MARKET_SUFFIX = (".SH", ".SZ", ".BJ")


def stock_query(symbol: str, name: str | None = None) -> str:
    """个股新闻查询词。港美股用英文模板, A 股用中文。"""
    sym = (symbol or "").strip()
    label = (name or "").strip()
    if sym.upper().endswith(_CN_MARKET_SUFFIX) or not sym:
        base = f"{label} {sym}".strip() if label else sym
        return f"{base} 最新消息 公告"
    return f"{label or sym} stock news"


def concept_query(topic: str) -> str:
    topic = (topic or "").strip()
    return f"{topic} 板块 最新消息" if topic else ""
