"""强度梯队 API — 港美"动量档位"梯队查询(替代 A 股连板梯队)。

A 股连板梯队维持现有独立表; 本批次新增 API 只服务 hk/us。
cn 路径返回 404(市场未实现)以避免前端误用。
"""
from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from app.services import strength_ladder

router = APIRouter(prefix="/api/strength_ladder", tags=["strength_ladder"])

_MARKET_PATTERN = "^(cn|hk|us)$"


@router.get("")
def get_strength_ladder(
    request: Request,
    market: Annotated[str, Query(pattern=_MARKET_PATTERN)] = "hk",
    target_date: date | None = Query(None, alias="date"),
    bands: Annotated[str | None, Query(description="逗号分隔 band 过滤, 例 'm25,m15'; 缺省全 4 档")] = None,
):
    """获取港美某日"动量档位"梯队。

    - market=cn: 400(本批次不覆盖 A 股连板梯队, 维持现状)
    - market=hk/us: 返回 4 档动量梯队 + 各档位 top N 标的
    - date: 目标交易日; 缺省 = 持久化最新一日
    - bands: 过滤档位, 逗号分隔; 缺省全 4 档
    """
    if market == "cn":
        raise HTTPException(
            status_code=400,
            detail="strength_ladder 仅服务港美 (hk/us); A 股连板梯队走 market_phase / monitor",
        )

    data_dir = request.app.state.repo.store.data_dir
    df = strength_ladder.load_strength_ladder_history(data_dir, market)
    if df.is_empty():
        return {
            "market": market,
            "date": target_date.isoformat() if target_date else None,
            "bands": {},
            "total_count": 0,
        }

    # 取目标日: 默认最新一日
    if target_date is None:
        latest = df["date"].max()
        target_date = latest if isinstance(latest, date) else date.fromisoformat(str(latest)[:10])
    df = df.filter(strength_ladder._date_col(df) == target_date)

    if df.is_empty():
        return {
            "market": market,
            "date": target_date.isoformat(),
            "bands": {},
            "total_count": 0,
        }

    grouped = strength_ladder.group_ladder_by_band(df)
    if bands:
        wanted = {b.strip() for b in bands.split(",") if b.strip()}
        grouped = {k: v for k, v in grouped.items() if k in wanted}

    total = sum(len(v) for v in grouped.values())
    return {
        "market": market,
        "date": target_date.isoformat(),
        "bands": grouped,
        "total_count": total,
    }