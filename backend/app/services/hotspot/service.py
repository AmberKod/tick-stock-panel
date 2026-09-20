"""热点业务逻辑层(discover / detail / refresh)。

把 Source + Storage 拼装成对外业务接口:
- ``discover_hotspots`` 返回主题列表,写 topics.parquet,必要时回退上一次 snapshot。
- ``get_hotspot_detail`` 返回单主题详情(含成分股),写 constituents/<topic>.parquet。
- ``refresh_hotspots`` 强制手动同步(被 POST /api/v1/hotspots/refresh 调用)。

与参考项目 ``src/services/screening/hotspot.py`` discover_hotspots 等价的
业务编排在这里实现;具体评分/分类在 ``scoring.py``、持久化在 ``storage.py``。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.hotspot.models import (
    QUALITY_FAILED,
    QUALITY_MISSING,
    QUALITY_OK,
    QUALITY_PARTIAL,
    QUALITY_STALE,
    HotspotDetail,
    HotspotResults,
    HotspotSummary,
    SourceError,
    utc_now_iso,
)
from app.services.hotspot.scoring import safe_float, safe_text
from app.services.hotspot.source import HotspotSource, select_source
from app.services.hotspot.storage import HotspotStorage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 入口函数(与参考项目同形 API:discover_hotspots/get_hotspot_detail)
# ---------------------------------------------------------------------------

def discover_hotspots(
    data_dir: Any,
    *,
    provider: str | None = None,
    market: str = "cn",
    top: int = 20,
    refresh: bool = False,
    source: HotspotSource | None = None,
    storage: HotspotStorage | None = None,
) -> HotspotResults:
    """顶层入口:发现热点主题列表,必要时回退持久化快照。

    Args:
        data_dir: 数据根目录(Path 或 str)。
        provider: provider 名称(暂作为占位参数,未来可与 akshare 双源兼容)。
        market: "cn" | "hk" | "us"(三个市场都有本地聚合源; 未知市场返回 missing_mapping)。
        top: 保留前 N 个主题。
        refresh: 本批占位参数; True/False 均先访问 source, 失败后读快照。
        source: 测试可注入桩。

    Returns:
        HotspotResults(含 metadata)。空结果带 missing_mapping 错误。
    """
    storage = storage or HotspotStorage(data_dir)
    src = source or select_source(market)
    if not src.supports(market):
        # 未知市场无 topic 数据源:fail-closed(与 concept_heat 风格一致)
        return _missing_market_result(market=market)

    # source 的 top=0 契约返回完整快照, 请求 top 只影响响应。
    fresh, failure_status = _discover_from_source(src, market)
    if fresh.is_usable:
        return _limit_results(_persist_and_return(fresh, storage, market=market), top)

    # 2) 拉新失败 → 回退持久化快照(只回退本市场分片)
    #    "" 仍保留: 迁移前写入的旧快照没有 snapshot_market, 归 A 股分片。
    cached = [item for item in storage.read_topics(market=market) if item.snapshot_market in {"", market}]
    age = _snapshot_age_hours(storage, cached, market=market) if cached else None
    previous_state = storage.read_job_state()
    cached_provider = next((item.provider_used for item in cached if item.provider_used), "")
    if not cached_provider and previous_state.get("last_status") == "success":
        cached_provider = safe_text(previous_state.get("provider_used"))
    _record_attempt(storage, market=market, status=failure_status, results=fresh)
    if cached:
        fallback = HotspotResults(
            cached,
            provider_used=cached_provider or "cache",
            fallback_used=True,
            source_errors=fresh.source_errors or ["live source failed; using last snapshot"],
            stale=True,
            stale_age_hours=age,
            market=market,
            quality_status=QUALITY_STALE,
        )
        return _limit_results(_normalize_quality(fallback), top)

    # 3) 完全无数据 → 空结果
    return HotspotResults(
        [],
        provider_used=fresh.provider_used or "none",
        source_errors=fresh.source_errors or ["no live rows and no cached snapshot"],
        market=market,
        quality_status=QUALITY_FAILED,
    )


def get_hotspot_detail(
    data_dir: Any,
    topic: str,
    *,
    market: str = "cn",
    top_stocks: int = 10,
    refresh: bool = False,
    source: HotspotSource | None = None,
    storage: HotspotStorage | None = None,
) -> HotspotDetail | None:
    """获取单主题详情。返回 None 表示 topic 不存在。"""
    storage = storage or HotspotStorage(data_dir)

    src = source or select_source(market)
    if not src.supports(market):
        return _missing_market_detail(topic=topic, market=market)

    detail = src.fetch_detail(topic, market=market, top_stocks=top_stocks)
    if detail is None:
        return None

    # 写详情 parquet + history
    if detail.stocks:
        # 成分股按 market 分片: 港美产出的是行业名、A 股是概念名, 撞名会互相整文件覆盖
        storage.write_constituents(detail.summary.topic, detail.stocks, market=market)
        storage.append_constituents_history(
            detail.summary.topic, detail.stocks, market=market,
        )

    return detail


# ---------------------------------------------------------------------------
# 内部协调
# ---------------------------------------------------------------------------

def _discover_from_source(source: HotspotSource, market: str) -> tuple[HotspotResults, str]:
    """在 provider 边界隔离失败, 供列表和刷新共用。"""
    try:
        return source.discover(market=market, top=0), "empty"
    except Exception as exc:
        provider = getattr(source, "name", "unknown")
        error = SourceError(provider=provider, method="discover", message=f"{type(exc).__name__}: {str(exc)[:200]}")
        logger.warning("hotspot source discovery failed: %s", error.as_str())
        return HotspotResults([], provider_used=provider, source_errors=[error.as_str()], market=market), "error"


def _normalize_quality(results: HotspotResults) -> HotspotResults:
    """拷贝源结果并统一容器和条目的降级信息, 不改写 provider 对象。"""
    result = deepcopy(results)
    result.provider_used = result.provider_used or "stub"
    result.stale = result.stale or result.quality_status == QUALITY_STALE or any(
        item.stale or item.quality_status == QUALITY_STALE for item in result
    )
    result.fallback_used = result.fallback_used or any(item.fallback_used for item in result)
    ages = [safe_float(age, default=-1.0) for age in [result.stale_age_hours, *(item.stale_age_hours for item in result)]]
    known_ages = [age for age in ages if age is not None and age >= 0.0]
    result.stale_age_hours = max(known_ages) if known_ages else None
    result.source_errors = list(dict.fromkeys([
        *result.source_errors,
        *(error for item in result for error in item.source_errors),
    ]))
    if result.stale:
        result.quality_status = QUALITY_STALE
    elif result.fallback_used or any(item.quality_status != QUALITY_OK or item.missing_fields for item in result):
        result.quality_status = QUALITY_PARTIAL
    for item in result:
        item.provider_used = item.provider_used or result.provider_used
        item.stale = result.stale
        item.fallback_used = result.fallback_used
        item.stale_age_hours = result.stale_age_hours
        item.source_errors = list(result.source_errors)
        if result.stale:
            item.quality_status = QUALITY_STALE
        elif result.fallback_used and item.quality_status == QUALITY_OK:
            item.quality_status = QUALITY_PARTIAL
    return result


def _parse_timestamp(value: str) -> datetime | None:
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp


def _persist_and_return(
    fresh: HotspotResults, storage: HotspotStorage, *, market: str = "cn",
) -> HotspotResults:
    """发布完整快照, 保留数据质量和观察时间; 任务时间另行记录。"""
    result = _normalize_quality(fresh)
    result.market = market
    now = datetime.now(UTC)
    for item in result:
        observed_at = _parse_timestamp(item.snapshot_at)
        if observed_at is None and result.stale_age_hours is not None:
            observed_at = now - timedelta(hours=result.stale_age_hours)
        if observed_at is None and not result.stale and not result.fallback_used:
            observed_at = now
        item.snapshot_at = observed_at.isoformat() if observed_at is not None else ""
        item.snapshot_market = market
        item.topic_date = item.topic_date or item.snapshot_at[:10]
    storage.write_topics(list(result), market=market)
    # market 必传: history 是三市场共用的一个 jsonl, 缺了它同名 topic 无法区分
    storage.append_history(result, market=market)
    status = "success" if result.quality_status == QUALITY_OK else "degraded"
    _record_attempt(storage, market=market, status=status, results=result)
    return result


def _snapshot_age_hours(
    storage: HotspotStorage, items: list[HotspotSummary], *, market: str,
) -> float | None:
    """只用与数据同次发布的观察时间, 或旧缓存有证据的成功时间。"""
    now = datetime.now(UTC)
    timestamps = [_parse_timestamp(item.snapshot_at) for item in items]
    if all(timestamp is not None for timestamp in timestamps):
        return round(max(max((now - timestamp).total_seconds() / 3600.0, 0.0) for timestamp in timestamps if timestamp is not None), 4)
    # 新格式显式记录未知观察时间时, 不能借用另一份快照的成功时刻。
    if any(item.snapshot_market for item in items):
        return None
    state = storage.read_job_state()
    timestamp = _parse_timestamp(state.get("last_success_at", {}).get(market, ""))
    if timestamp is None:
        return None
    return round(max((now - timestamp).total_seconds() / 3600.0, 0.0), 4)


def _limit_results(result: HotspotResults, top: int) -> HotspotResults:
    return HotspotResults(
        list(result[:top] if top > 0 else result), provider_used=result.provider_used,
        fallback_used=result.fallback_used, source_errors=result.source_errors,
        stale=result.stale, stale_age_hours=result.stale_age_hours, market=result.market,
        quality_status=result.quality_status,
    )


def _record_attempt(
    storage: HotspotStorage, *, market: str, status: str, results: HotspotResults,
) -> None:
    storage.write_job_state({
        "last_run": utc_now_iso(), "last_status": status, "rows": len(results),
        "provider_used": results.provider_used, "markets": {market: len(results)},
        "source_errors": list(results.source_errors),
    })


def _missing_market_result(*, market: str) -> HotspotResults:
    """未知市场(非 cn/hk/us):fail-closed,返回 missing_mapping。"""
    err = SourceError(
        provider="hotspot",
        method="discover",
        message=f"market '{market}' has no topic data source",
    )
    return HotspotResults(
        [],
        provider_used="none",
        source_errors=[err.as_str()],
        market=market,
        quality_status=QUALITY_MISSING,
    )


def _missing_market_detail(topic: str, *, market: str) -> HotspotDetail:
    err = SourceError(
        provider="hotspot",
        method="fetch_detail",
        message=f"market '{market}' has no topic data source",
    )
    summary = HotspotSummary(
        topic=topic,
        name=topic,
        source="",
        heat_score=0.0,
        quality_status=QUALITY_MISSING,
        missing_fields=["market", "source", "stocks", "leader_stocks", "timeline", "route"],
        provider_used="none",
    )
    summary.source_errors = [err.as_str()]
    return HotspotDetail(
        summary=summary,
        stocks=[],
        timeline=[],
        route=[],
        stock_count=0,
    )


# ---------------------------------------------------------------------------
# 同步 + 状态
# ---------------------------------------------------------------------------

def refresh_hotspots(
    data_dir: Any,
    *,
    market: str = "cn",
    source: HotspotSource | None = None,
    storage: HotspotStorage | None = None,
) -> dict[str, Any]:
    """手动触发一次 sync,更新 <market>/topics.parquet 与 job_state。

    Returns:
        简易状态字典,用于 API 响应:
        status 为 ok / degraded / empty / error / skipped, 并包含 rows 和 provider。
    """
    storage = storage or HotspotStorage(data_dir)
    src = source or select_source(market)
    if not src.supports(market):
        storage.write_job_state({
            "last_run": utc_now_iso(),
            "last_status": "skipped",
            "rows": 0,
            "provider_used": "none",
            "markets": {market: 0},
            "message": f"market {market!r} has no topic data source",
        })
        return {"status": "skipped", "rows": 0, "provider": "none"}

    fresh, failure_status = _discover_from_source(src, market)
    if not fresh.is_usable:
        _record_attempt(storage, market=market, status=failure_status, results=fresh)
        return {"status": failure_status, "rows": 0, "provider": fresh.provider_used}

    fresh = _persist_and_return(fresh, storage, market=market)
    return {
        "status": "ok" if fresh.quality_status == QUALITY_OK else "degraded",
        "rows": len(fresh),
        "provider": fresh.provider_used,
        "last_run": utc_now_iso(),
    }


# ---------------------------------------------------------------------------
# 类包装(便于热路径上挂 storage)
# ---------------------------------------------------------------------------

class HotspotService:
    """把 storage 与 source 合一,方便路由层注入。"""

    def __init__(
        self,
        data_dir: Any,
        *,
        storage: HotspotStorage | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.storage = storage or HotspotStorage(data_dir)

    def discover(
        self,
        *,
        market: str = "cn",
        top: int = 20,
        refresh: bool = False,
        source: HotspotSource | None = None,
    ) -> HotspotResults:
        return discover_hotspots(
            self.data_dir,
            market=market,
            top=top,
            refresh=refresh,
            source=source,
            storage=self.storage,
        )

    def detail(
        self,
        topic: str,
        *,
        market: str = "cn",
        top_stocks: int = 10,
        source: HotspotSource | None = None,
    ) -> HotspotDetail | None:
        return get_hotspot_detail(
            self.data_dir,
            topic,
            market=market,
            top_stocks=top_stocks,
            source=source,
            storage=self.storage,
        )

    def refresh(
        self,
        *,
        market: str = "cn",
        source: HotspotSource | None = None,
    ) -> dict[str, Any]:
        return refresh_hotspots(
            self.data_dir,
            market=market,
            source=source,
            storage=self.storage,
        )

    def job_state(self) -> dict[str, Any]:
        return self.storage.read_job_state()
