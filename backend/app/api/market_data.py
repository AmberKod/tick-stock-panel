from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any

from app.data_providers.normalizer import normalize_market_symbols
from app.services import kline_sync
from app.services.market_daily_sync import load_market_universe


async def start_market_download(
    request: Any, market: str, symbols: str | None, start: date | None, end: date | None,
) -> dict:
    """Validate the HTTP request then use the existing checkpoint-backed job."""
    from fastapi import HTTPException

    from app.api.pipeline import _start_market_daily_job
    from app.markets import get_profile

    repo = request.app.state.repo
    capset = getattr(request.app.state, "capabilities", None)
    try:
        selected = (
            load_market_universe(repo.store.data_dir, market) if symbols is None
            else normalize_market_symbols([value.strip() for value in symbols.split(",") if value.strip()], market)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    end_date = end or get_profile(market).today()
    start_date = start or end_date - timedelta(days=365)
    if start_date > end_date:
        raise HTTPException(status_code=400, detail="起始日期不能晚于截止日期")
    provider, supported, reason = kline_sync.daily_sync_capability(capset, market)
    from app.services.market_data_status import market_data_generation
    base = {"operation": "daily_download", "market": market, "requested": len(selected),
            "succeeded": 0, "failed": 0, "skipped": 0, "enriched_dates_written": 0,
            "provider": provider, "failures": [],
            "data_generation": market_data_generation(repo.store.data_dir, market)}
    if not selected:
        return {**base, "status": "empty", "items": [], "message": "没有请求标的"}
    if not supported:
        return {**base, "status": "unsupported", "skipped": len(selected), "message": reason,
                "failures": [{"symbol": symbol, "reason": reason} for symbol in selected],
                "items": [{"symbol": symbol, "status": "skipped", "reason": reason} for symbol in selected]}
    result = await _start_market_daily_job(
        request, market=market, symbols=selected,
        start_date=datetime.combine(start_date, datetime_time.min),
        end_date=datetime.combine(end_date, datetime_time.max),
        mode="incremental", batch_size=None,
    )
    if result["status"] == "reused":
        raise HTTPException(status_code=409, detail="已有数据任务正在运行,请在任务列表查看完成状态")
    return {**base, **result, "items": []}


def refresh_market_state(request: Any, market: str) -> None:
    """Refresh existing storage and market consumers after one logical publish."""
    from app.api.data import invalidate_storage_cache

    invalidate_storage_cache()
    repo = getattr(request.app.state, "repo", None)
    refresh = getattr(repo, "invalidate_market_cache", None)
    if callable(refresh):
        refresh(market.lower())


async def start_hk_financial_sync(request: Any, symbols: str | None) -> dict:
    from fastapi import HTTPException

    from app.api.pipeline import _start_hk_financial_job
    from app.services.market_data_status import hk_financial_capability, market_data_generation

    try:
        selected = None if symbols is None else normalize_market_symbols(
            [symbol.strip() for symbol in symbols.split(",") if symbol.strip()], "HK",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    base = {"market": "HK", "operation": "financial_sync", "requested": len(selected or []),
            "succeeded": 0, "failed": 0, "skipped": 0, "items": [], "failures": [],
            "enriched_dates_written": 0,
            "data_generation": market_data_generation(request.app.state.repo.store.data_dir, "HK")}
    if selected == []:
        return {**base, "status": "empty", "message": "没有请求标的"}
    supported, reason = hk_financial_capability()
    if not supported:
        return {**base, "status": "unsupported", "skipped": len(selected or []), "message": reason}
    result = await _start_hk_financial_job(request, symbols=selected)
    if result["status"] == "reused":
        raise HTTPException(status_code=409, detail="已有数据任务正在运行, 请在任务列表查看完成状态")
    return {**base, **result}

