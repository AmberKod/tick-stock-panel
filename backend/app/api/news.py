"""新闻/舆情 API — 热点工作区的"为什么涨"那一维。

热点页原本只有本地行情聚合出的概念热度, 回答不了"今天发生了什么"。
本模块接外部搜索源(当前 Anspire)补新闻/公告/事件。

密钥: 设置页写入的 secrets.json 优先, 否则环境变量 ANSPIRE_API_KEYS。
未配 Key 时接口照常返回 success=false + 明确原因, 前端据此显示"去配置",
而不是静默给个空列表让用户以为是没新闻。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.services import news_feed, news_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])

# ── 批量归因: 限速 + 缓存 ──────────────────────────────────────
# 背景: 异动归因面板要对 N 条异动逐条查新闻, 而新闻源带配额(rpm)。
# 逐条并发 = N 次外部调用直接打爆配额。因此批量端点必须:
#   ① 内部串行 (全局闸门), ② 两次真实调用之间限速, ③ 结果按天缓存。
# 缓存命中不消耗配额, 也不占用限速间隔。
_BATCH_MIN_INTERVAL_S = 0.6      # 两次真实搜索之间的最小间隔 (≈≤100 次/分钟)
_BATCH_MAX_SYMBOLS = 200         # 单批上限, 防止一次请求拖死整个进程
_BATCH_TIME_BUDGET_S = 25.0      # 单批时间预算, 超预算的剩余标的标记未查询(部分可用)
_BATCH_CACHE_TTL = 3600.0        # 结果缓存 TTL(键含当天日期, 换天自动失效)
# 失败结果的 TTL **故意短得多**: 若失败也缓存 1 小时, 用户补好 Key / 配额恢复后
# 点「重试」拿到的仍是缓存里的失败结果, 面板会一直卡在「不可用」——
# 缓存的本意是"防止重试打爆配额", 不是"把失败钉死一小时"。
_BATCH_CACHE_FAIL_TTL = 60.0

_batch_cache: dict[tuple, tuple[float, dict]] = {}
_batch_cache_lock = threading.Lock()


def _cache_ttl_for(entry: dict) -> float:
    """缓存条目的 TTL: 成功长、失败短。

    失败用短 TTL 是刻意的 —— 缓存的目的是"防止重试打爆配额", 不是"把失败
    钉死一小时"。失败若也缓存 1 小时, provider 恢复正常后前端拿到的仍是旧失败
    结果, 面板会一直显示「不可用」且重试无效。
    """
    return _BATCH_CACHE_TTL if entry.get("success") else _BATCH_CACHE_FAIL_TTL
# 串行闸门: 同一时刻只允许一个批次在跑, 杜绝"多请求叠加打爆配额"。
_batch_gate = threading.Lock()
_batch_last_call_ts = 0.0

# 面板 2 的判定口径是「≥2 条不同域名命中」, 因为 news_search 的 source 是纯域名、
# 没做出版方家族归一。端点只返回去重后的域名列表, 不返回"独立来源数"这种
# 假装归一过的数字。
_DOMAIN_NOTE = (
    "hit_domains 为 URL 域名去重结果, 未做出版方家族归一: "
    "同一媒体多个站点可能被重复计数。判定请用「不同域名数 ≥ 2」, 不要当成独立信源数。"
)


def invalidate_batch_cache() -> None:
    """清空批量归因缓存(改 Key / 强制刷新时调用)。"""
    with _batch_cache_lock:
        _batch_cache.clear()


def _lookup_name(symbol: str) -> str | None:
    """从 A 股维表取股票简称(让查询词带上中文名, 召回更好)。"""
    from pathlib import Path

    import polars as pl

    from app.config import settings

    try:
        path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
        if not path.exists():
            return None
        # 精确取 A 股维表文件: 目录下还有 hk/us 两份, glob 一起扫会 schema 冲突
        df = pl.read_parquet(path, columns=["symbol", "name"])
        hit = df.filter(pl.col("symbol") == symbol)
        if hit.height:
            return str(hit["name"][0] or "") or None
    except Exception as e:
        logger.warning("news: 股票简称查询失败 (%s): %s", symbol, e)
    return None


@router.get("/search")
def search_news(
    query: str = Query(..., min_length=1, max_length=200),
    days: int = Query(7, ge=1, le=30),
    max_results: int = Query(8, ge=1, le=50),
) -> dict[str, Any]:
    """自由词搜索。"""
    return news_search.search(query, max_results=max_results, days=days).to_dict()


@router.get("/stock")
def stock_news(
    symbol: str = Query(..., min_length=2, max_length=32),
    name: str | None = Query(None, max_length=64),
    days: int = Query(7, ge=1, le=30),
    max_results: int = Query(8, ge=1, le=50),
) -> dict[str, Any]:
    """个股新闻。name 省略时自动从维表补, 让查询词带上中文名。"""
    label = name or _lookup_name(symbol)
    q = news_search.stock_query(symbol, label)
    payload = news_search.search(q, max_results=max_results, days=days).to_dict()
    payload["symbol"] = symbol
    payload["stock_name"] = label
    return payload


class BatchStockNewsRequest(BaseModel):
    """批量个股新闻归因请求体。"""

    symbols: list[str] = Field(default_factory=list, description="标的 symbol 列表")
    names: dict[str, str] = Field(
        default_factory=dict, description="symbol → 中文简称, 可省(省则查本地维表)"
    )
    days: int = Field(7, ge=1, le=30, description="回溯窗口天数")
    max_results: int = Field(8, ge=1, le=50, description="每个标的最大返回条数")


def _domains_of(hits: list[dict]) -> list[str]:
    """按出现顺序去重域名(纯域名, 不做出版方归一 — 见 _DOMAIN_NOTE)。"""
    domains: list[str] = []
    for hit in hits:
        domain = news_search._extract_domain(str(hit.get("url") or "")) or "未知来源"
        if domain not in domains:
            domains.append(domain)
    return domains


def _batch_entry(
    symbol: str,
    name: str | None,
    days: int,
    max_results: int,
    *,
    search_fn=None,
) -> tuple[dict, bool]:
    """单个标的的新闻归因。

    缓存 TTL 按成败分档: 成功缓存 1 小时, **失败只缓存 60 秒** —— 否则用户补好
    Key / 配额恢复后点「重试」仍会读到旧的失败结果, 面板永远卡在「不可用」。

    Returns:
        ``(entry, did_call)`` —— did_call=True 表示本次真的打了外部搜索源
        (调用方据此做限速), False 表示走了缓存(不消耗配额)。
    """
    search = search_fn or news_search.search
    label = name or _lookup_name(symbol)
    query = news_search.stock_query(symbol, label)
    # 市场时钟·B类: 这里要的是"服务器本地这一自然日"(缓存按宿主机自然日翻篇),
    # 不是任何市场交易日; 改成市场日期会让美股标的的缓存在美东翻篇时与宿主机
    # 自然日错位, 同一批新闻一天内被重复拉取(配额翻倍)或提前失效。
    cache_key = (symbol, label or "", days, max_results, date.today().isoformat())

    now = time.monotonic()
    with _batch_cache_lock:
        hit = _batch_cache.get(cache_key)
        if hit and (now - hit[0]) < _cache_ttl_for(hit[1]):
            cached = dict(hit[1])
            cached["cached"] = True
            return cached, False

    try:
        resp = search(query, max_results=max_results, days=days)
    except Exception as e:
        logger.warning("news batch: %s 搜索异常: %s", symbol, e)
        entry = {
            "symbol": symbol,
            "stock_name": label,
            "query": query,
            "success": False,
            "error": f"搜索异常: {e}",
            "result_count": 0,
            "hit_domains": [],
            "hit_domain_count": 0,
            "hits": [],
            "cached": False,
        }
        return entry, True

    hits = [item.to_dict() for item in (getattr(resp, "results", None) or [])]
    domains = _domains_of(hits)
    entry = {
        "symbol": symbol,
        "stock_name": label,
        "query": query,
        "provider": str(getattr(resp, "provider", "") or ""),
        "success": bool(getattr(resp, "success", False)),
        "error": getattr(resp, "error_message", None) if not getattr(resp, "success", False) else None,
        "result_count": len(hits),
        "hit_domains": domains,
        "hit_domain_count": len(domains),
        "hits": hits,
        "cached": False,
    }
    with _batch_cache_lock:
        _batch_cache[cache_key] = (time.monotonic(), dict(entry))
    return entry, True


@router.post("/batch-stock")
def batch_stock_news(payload: BatchStockNewsRequest) -> dict[str, Any]:
    """批量个股新闻归因(面板 2「异动归因」的可行性前提)。

    一次入参 N 个 symbol, **内部串行 + 限速**调用底层搜索源, 避免 N 次并发
    打爆配额; 结果按 (symbol + 窗口 + 当天日期) 缓存, 同一标的重复查询不再
    消耗配额。

    部分可用: 单个 symbol 出错只标记该 symbol 的 error, 不影响其他 symbol。

    Returns:
        ``{ok, results:{symbol:{hits,hit_domains,...}}, ...}`` ——
        ``hit_domains`` 为**去重后的域名列表**, 调用方自行数"几个不同域名"。
    """
    global _batch_last_call_ts

    requested = [str(s).strip() for s in (payload.symbols or []) if str(s).strip()]
    # 去重保序
    symbols: list[str] = list(dict.fromkeys(requested))
    results: dict[str, dict] = {}

    started = time.monotonic()
    with _batch_gate:
        for index, symbol in enumerate(symbols):
            elapsed = time.monotonic() - started
            if index >= _BATCH_MAX_SYMBOLS:
                results[symbol] = {
                    "symbol": symbol,
                    "stock_name": payload.names.get(symbol),
                    "query": None,
                    "success": False,
                    "error": f"超出单批上限 {_BATCH_MAX_SYMBOLS}, 未查询",
                    "result_count": 0,
                    "hit_domains": [],
                    "hit_domain_count": 0,
                    "hits": [],
                    "cached": False,
                }
                continue
            if elapsed > _BATCH_TIME_BUDGET_S:
                results[symbol] = {
                    "symbol": symbol,
                    "stock_name": payload.names.get(symbol),
                    "query": None,
                    "success": False,
                    "error": f"批次时间预算 {_BATCH_TIME_BUDGET_S:.0f}s 用尽, 未查询",
                    "result_count": 0,
                    "hit_domains": [],
                    "hit_domain_count": 0,
                    "hits": [],
                    "cached": False,
                }
                continue

            entry, did_call = _batch_entry(
                symbol,
                payload.names.get(symbol),
                payload.days,
                payload.max_results,
            )
            results[symbol] = entry
            # 限速: 两次真实搜索之间至少间隔 _BATCH_MIN_INTERVAL_S。
            # 批内最后一个标的之后的等待没有意义(后面没有调用要保护), 跳过以省下 0.6s。
            if did_call and index < len(symbols) - 1:
                wait = _BATCH_MIN_INTERVAL_S - (time.monotonic() - _batch_last_call_ts)
                if wait > 0:
                    time.sleep(wait)
            if did_call:
                _batch_last_call_ts = time.monotonic()

    provider = next(
        (str(e.get("provider") or "") for e in results.values() if e.get("provider")),
        "",
    )
    return {
        "ok": True,
        "domain_note": _DOMAIN_NOTE,
        "days": payload.days,
        "max_results": payload.max_results,
        "requested": len(requested),
        "processed": len(results),
        "cached_count": sum(1 for e in results.values() if e.get("cached")),
        "error_count": sum(1 for e in results.values() if not e.get("success")),
        "elapsed_s": round(time.monotonic() - started, 3),
        "throttle": {
            "serial": True,
            "min_interval_s": _BATCH_MIN_INTERVAL_S,
            "max_symbols": _BATCH_MAX_SYMBOLS,
            "time_budget_s": _BATCH_TIME_BUDGET_S,
        },
        "provider": provider,
        "results": results,
    }


@router.get("/concept")
def concept_news(
    topic: str = Query(..., min_length=1, max_length=64),
    days: int = Query(7, ge=1, le=30),
    max_results: int = Query(8, ge=1, le=50),
) -> dict[str, Any]:
    """概念/板块新闻。"""
    q = news_search.concept_query(topic)
    payload = news_search.search(q, max_results=max_results, days=days).to_dict()
    payload["topic"] = topic
    return payload


@router.get("/status")
def news_status(request: Request) -> dict[str, Any]:
    """搜索源配置状态(供前端决定显示"配置"还是"搜索")。"""
    from app import secrets_store

    out: dict[str, Any] = {"providers": []}
    for name, cls in news_search.PROVIDER_CLASSES.items():
        raw = secrets_store.get_env_backed_secret(cls.secret_name, cls.secret_name.upper())
        keys = news_search._split_keys(raw)
        out["providers"].append({
            "name": name,
            "label": cls.__doc__.split("\n")[0].strip() if cls.__doc__ else name,
            "configured": bool(keys),
            "key_count": len(keys),
            "masked": secrets_store.mask(keys[0]) if keys else "",
        })
    out["configured_any"] = any(p["configured"] for p in out["providers"])
    return out


@router.get("/categories")
def news_categories() -> dict[str, Any]:
    """新闻分类清单(含 RSS 分类与走检索源的"股市·个股")。"""
    return {"categories": news_feed.categories_payload()}


@router.get("/feeds")
def list_feeds(
    category: str = Query("world", min_length=1, max_length=32),
    hours: int = Query(48, ge=0, le=24 * 30),
    limit: int = Query(40, ge=1, le=200),
    refresh: bool = Query(False, description="跳过缓存强制重抓"),
) -> dict[str, Any]:
    """RSS 新闻流(不需要 API Key)。

    抓不到任何源时返回 success=false + source_errors, 前端据此显示"源不可用",
    而不是渲染一个空列表冒充"没新闻"。
    """
    result = news_feed.fetch_category(category, hours=hours, limit=limit, use_cache=not refresh)
    return result.to_dict()


@router.post("/cache/invalidate")
def invalidate_cache() -> dict[str, bool]:
    news_search.invalidate_cache()
    news_feed.invalidate_cache()
    invalidate_batch_cache()
    return {"ok": True}
