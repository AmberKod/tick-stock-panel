"""美股基本面 API — SEC EDGAR (九章 us_fund 摘取, P0-2)。

- GET /api/us/financials/{symbol}: 返回 fundamentals + source_gate 校验。
- 项目惯例: 不可用是正常状态不是错误 → 失败也返回 HTTP 200,
  ``{available: false, reason: ..., source: "sec-edgar"}``,
  绝不返回占位值伪装成功。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.data_providers.jiuzhang_us_fund_provider import SOURCE, JiuzhangUSFundProvider
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/us/financials", tags=["us-financials"])

_provider = JiuzhangUSFundProvider()


@router.get("/{symbol}")
def get_us_financials(symbol: str) -> dict:
    """单只美股基本面 (SEC EDGAR XBRL, 官方免费)。

    - 成功: ``{available: true, source: "sec-edgar", symbol, ...metrics}``
    - provider 抛错/超时/无数据: HTTP 200 + ``available: false`` +
      具体原因 (fail-closed, 不占位)。
    """
    try:
        data = _provider.fundamentals(symbol)
    except Exception as exc:
        logger.warning("us financials provider error %s: %s", symbol, exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": SOURCE}

    if not data:
        return {"available": False, "reason": "no_data: 非美股或 SEC 无该标的报表数据", "source": SOURCE}

    if not is_real(data):
        # 理论不可达: provider 恒带 source=sec-edgar。防御性闸门 —— 一旦
        # 未来 provider 被改坏, 假数据在这里被拦下而不是流向前端。
        logger.error("us financials blocked by source_gate: %s source=%r", symbol, data.get("source"))
        return {"available": False, "reason": f"source_gate_rejected: {data.get('source')!r}", "source": SOURCE}

    logger.info("us financials %s: %d metrics from %s", symbol, data.get("count", 0), SOURCE)
    return {
        "available": True,
        "source": data["source"],
        "symbol": data["ticker"],
        "cik": data["cik"],
        "count": data.get("count", 0),
        "metrics": data["metrics"],
    }
