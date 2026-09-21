"""热点工作区 API — /api/v1/hotspots/*。

本批次只提供路由 + Stub 数据接入骨架:

- GET  /api/v1/hotspots                          主题列表(含 quality 元数据)
- GET  /api/v1/hotspots/{topic}                  单主题详情(含成分股)
- POST /api/v1/hotspots/refresh                   手动触发同步(今日为 stub,下一批接 akshare)
- GET  /api/v1/hotspots/job-state                 最近一次同步状态

接口契约(响应 JSON 结构)尽量对齐参考项目
``src/services/screening/hotspot.py`` 与前端 ``dsa-web/src/api/screening.ts``,
便于下一批前端直接复用。``market`` 仅支持 ``"cn"``,港美返回
``quality_status="missing_mapping"`` 的空列表或 detail(fail-closed,与
``app/strategy/concept_heat.py`` 同语义)。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.services.hotspot import (
    HotspotDetail,
    HotspotService,
    HotspotSource,
    HotspotStock,
    HotspotSummary,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/hotspots", tags=["hotspots"])

_ALLOWED_MARKETS = {"cn", "hk", "us"}


def _data_dir(request: Request) -> Any:
    """取 repo 的 data_dir;与现有 API 风格一致。"""
    repo = getattr(request.app.state, "repo", None)
    if repo is not None and getattr(repo, "store", None) is not None:
        return repo.store.data_dir
    from pathlib import Path
    return Path.cwd() / "data"


def _resolve_source(market: str, request: Request) -> HotspotSource | None:
    """从 app.state 取 source override;没有就 fallback 到默认选择器。

    下一批接入时可以由 lifespan 注册 ``app.state.hotspot_cn_source = AkshareSource()``,
    本批先返回 None 给 service 让它走默认 stub。
    """
    override_key = f"hotspot_{market}_source"
    return getattr(request.app.state, override_key, None)


# ---------------------------------------------------------------------------
# GET /api/v1/hotspots
# ---------------------------------------------------------------------------

@router.get("")
def list_hotspots(
    request: Request,
    market: str = Query("cn", min_length=1, max_length=8),
    top: int = Query(20, ge=1, le=200),
    refresh: bool = Query(False, description="占位参数, 本批始终先请求源, 失败时回退快照"),
    include_details: bool = Query(False, description="是否同时返回每个 topic 的明细(今日仅 stub 路径能产出)"),
):
    """主题列表(含 quality 元数据)。

    返回结构对齐参考项目 ``ScreeningHotspotsResponse``,但简化 metadata 字段。
    """
    if market not in _ALLOWED_MARKETS:
        raise HTTPException(400, f"market 必须为 {sorted(_ALLOWED_MARKETS)} 之一")

    data_dir = _data_dir(request)
    src = _resolve_source(market, request)
    service = HotspotService(data_dir)
    results = service.discover(
        market=market,
        top=top,
        refresh=refresh,
        source=src,
    )

    payload: dict[str, Any] = {
        "enabled": True,
        "provider": results.provider_used or "stub",
        "provider_used": results.provider_used or "stub",
        "fallback_used": results.fallback_used,
        "cache_used": results.fallback_used,
        "cached_at": results.stale_age_hours,
        "stale": results.stale,
        "stale_age_hours": results.stale_age_hours,
        "quality_status": results.quality_status,
        "source_errors": list(results.source_errors),
        # 样本覆盖率 {"covered","universe","ratio","as_of","stale_symbols","stale_as_of"}
        # None = 分母不可得 / 该源不适用 → 前端不显示, 不用缺省值伪装成"全量样本"
        "sample_coverage": results.sample_coverage,
        "market": results.market,
        "hotspots": [summary_to_dict(item) for item in results],
        "hotspot_count": len(results),
        "details": {},
    }

    if include_details:
        details: dict[str, dict[str, Any]] = {}
        for item in results:
            inner = service.detail(item.topic, market=market, top_stocks=10, source=src)
            details[item.topic] = detail_to_dict(inner) if inner else {"missing": True}
        payload["details"] = details
    return payload


# ---------------------------------------------------------------------------
# GET /api/v1/hotspots/job-state  (必须在 {topic:path} 之前注册)
# ---------------------------------------------------------------------------

@router.get("/job-state")
def job_state(request: Request):
    """最近一次同步状态(与 storage.job_state 字段对齐)。"""
    data_dir = _data_dir(request)
    state = HotspotService(data_dir).job_state()
    return state


# ---------------------------------------------------------------------------
# POST /api/v1/hotspots/refresh
# ---------------------------------------------------------------------------

@router.post("/refresh")
def refresh(request: Request, market: str = Query("cn", min_length=1, max_length=8)):
    """手动触发同步(下一批接 akshare,本批为 stub 通路占位)。"""
    if market not in _ALLOWED_MARKETS:
        raise HTTPException(400, f"market 必须为 {sorted(_ALLOWED_MARKETS)} 之一")
    data_dir = _data_dir(request)
    src = _resolve_source(market, request)
    service = HotspotService(data_dir)
    result = service.refresh(market=market, source=src)
    return result


# ---------------------------------------------------------------------------
# GET /api/v1/hotspots/{topic}  (path 路由放在最后,避免吞掉子路径)
# ---------------------------------------------------------------------------

@router.get("/{topic:path}")
def get_hotspot(
    topic: str,
    request: Request,
    market: str = Query("cn", min_length=1, max_length=8),
    include_search: bool = Query(False, description="是否搜新闻,今日 stub 不支持"),
):
    """单主题详情(含成分股)。"""
    if market not in _ALLOWED_MARKETS:
        raise HTTPException(400, f"market 必须为 {sorted(_ALLOWED_MARKETS)} 之一")
    data_dir = _data_dir(request)
    src = _resolve_source(market, request)
    service = HotspotService(data_dir)
    detail = service.detail(topic, market=market, top_stocks=10, source=src)
    if detail is None:
        raise HTTPException(404, f"topic '{topic}' 在 market '{market}' 中不存在")
    return detail_to_dict(detail)


# ---------------------------------------------------------------------------
# 序列化(service.models → API JSON)
# ---------------------------------------------------------------------------

def summary_to_dict(item: HotspotSummary) -> dict[str, Any]:
    """HotspotSummary → API 响应 dict。"""
    return {
        "topic": item.topic,
        "name": item.name or item.topic,
        "source": item.source,
        "rank": item.rank,
        "change_pct": item.change_pct,
        "heat_score": item.heat_score,
        "trend_score": item.trend_score,
        "persistence_score": item.persistence_score,
        "cooling_score": item.cooling_score,
        "observations": item.observations,
        "state": item.state,
        # stage 允许为 None (JSON null): 趋势三维度没有观测值 → 未判定,
        # 前端据此不渲染阶段徽标, 而不是显示一个假的"初次异动"。
        "stage": item.stage,
        "sample_stock_count": item.sample_stock_count,
        "leaders": list(item.leaders or []),
        "leader_stocks": [stock_to_dict(s) for s in (item.leader_stocks or [])],
        "quality_status": item.quality_status,
        "missing_fields": list(item.missing_fields or []),
        "provider_used": item.provider_used,
        "fallback_used": item.fallback_used,
        "source_errors": list(item.source_errors or []),
        "stale": item.stale,
        "stale_age_hours": item.stale_age_hours,
        "topic_date": item.topic_date,
        "snapshot_at": item.snapshot_at,
        "snapshot_market": item.snapshot_market,
        "canonical_topic": item.canonical_topic or item.topic,
        "aliases": list(item.aliases or []),
    }


def stock_to_dict(stock: HotspotStock) -> dict[str, Any]:
    return {
        "code": stock.code,
        "name": stock.name,
        "change_pct": stock.change_pct,
        "amount": stock.amount,
        "turnover_rate": stock.turnover_rate,
        "volume_ratio": stock.volume_ratio,
        "net_inflow": stock.net_inflow,
        "is_limit_up": stock.is_limit_up,
        "active_days": stock.active_days,
        "evidence_count": stock.evidence_count,
        "role": stock.role,
        "hot_stock_score": stock.hot_stock_score,
        "source": stock.source,
        "source_confidence": stock.source_confidence,
        "fallback_used": stock.fallback_used,
    }


def detail_to_dict(detail: HotspotDetail) -> dict[str, Any]:
    return {
        "enabled": True,
        "provider": detail.summary.provider_used or "stub",
        "topic": detail.summary.topic,
        "name": detail.summary.name,
        "canonical_topic": detail.summary.canonical_topic,
        "aliases": list(detail.summary.aliases or []),
        "summary": summary_to_dict(detail.summary),
        "route": list(detail.route or []),
        "timeline": list(detail.timeline or []),
        "stocks": [stock_to_dict(s) for s in detail.stocks],
        "leader_stocks": [stock_to_dict(s) for s in detail.summary.leader_stocks],
        "stock_count": detail.stock_count,
        "quality_status": detail.summary.quality_status,
        "missing_fields": list(detail.summary.missing_fields or []),
        "provider_used": detail.summary.provider_used,
        "fallback_used": detail.summary.fallback_used,
        "source_errors": list(detail.summary.source_errors or []),
        "stale": detail.summary.stale,
        "stale_age_hours": detail.summary.stale_age_hours,
        "cache_used": detail.summary.fallback_used,
        "news_search_requested": False,
        "news_search_status": "unavailable",
    }


# 对外供 lifespan / 测试校验 source 已挂载。
__all__ = ["detail_to_dict", "router", "stock_to_dict", "summary_to_dict"]
