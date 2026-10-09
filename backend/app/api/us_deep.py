"""美股深度数据 API — OpenBB (九章融合 P1)。

- GET /api/us/deep/quote/{symbol}: 实时盘口 (bid/ask/均线/年内高低)
- GET /api/us/deep/statement/{symbol}: 三大报表 (income|balance|cash)
- GET /api/us/deep/filings/{symbol}: SEC 报送列表
- GET /api/us/deep/summary/{symbol}: 盘口摘要文本

- 项目惯例(与 us_financials.py 一致): 不可用是正常状态不是错误 →
  失败也返回 HTTP 200, ``{available: false, reason: ..., source: "openbb"}``,
  绝不返回占位值伪装成功 (fail-closed)。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from app.data_providers.jiuzhang_openbb_provider import SOURCE, JiuzhangOpenBBProvider
from app.services.source_gate import is_real

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/us/deep", tags=["us-deep"])

_provider = JiuzhangOpenBBProvider()

_MARKETS = ("US", "HK")


def _norm_market(market: str) -> str:
    m = (market or "US").strip().upper()
    return m if m in _MARKETS else "US"


def _respond(symbol: str, market: str, call) -> dict:
    """统一响应: 成功过闸门 → available:true; 异常/无数据/被拦 → HTTP 200 + available:false。

    ``call`` 是 ``() -> {"source": ..., "data": ...}`` 的 provider 调用。
    """
    try:
        payload = call()
    except Exception as exc:
        logger.warning("us deep provider error (%s %s): %s", symbol, market, exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": SOURCE}

    data = payload.get("data")
    empty = not data or data == {} or data == []
    if empty:
        return {"available": False, "reason": "no_data: OpenBB 无该标的数据", "source": SOURCE}

    if not is_real(payload):
        # 理论不可达: provider 恒带 source=openbb。防御性闸门 —— 一旦
        # 未来 provider 被改坏, 假数据在这里被拦下而不是流向前端。
        logger.error("us deep blocked by source_gate: %s source=%r", symbol, payload.get("source"))
        return {"available": False, "reason": f"source_gate_rejected: {payload.get('source')!r}", "source": SOURCE}

    return {
        "available": True,
        "source": payload["source"],
        "symbol": symbol,
        "market": market,
        "data": data,
    }


@router.get("/quote/{symbol}")
def get_quote(symbol: str, market: str = "US") -> dict:
    """实时盘口: 最新价/bid/ask/50日·200日均线/年内高低。"""
    m = _norm_market(market)
    return _respond(symbol, m, lambda: _provider.quote(symbol, market=m))


@router.get("/statement/{symbol}")
def get_statement(symbol: str, kind: str = "income", period: str = "annual",
                  limit: int = 3) -> dict:
    """三大报表: kind=income|balance|cash, period=annual|quarter, limit=期数。"""
    if kind not in ("income", "balance", "cash"):
        return {"available": False, "reason": f"bad_kind: {kind!r} (income|balance|cash)", "source": SOURCE}
    if period not in ("annual", "quarter"):
        return {"available": False, "reason": f"bad_period: {period!r} (annual|quarter)", "source": SOURCE}
    if limit < 1 or limit > 12:
        return {"available": False, "reason": f"bad_limit: {limit} (1..12)", "source": SOURCE}
    return _respond(symbol, "US",
                    lambda: _provider.statement(symbol, kind=kind, period=period, limit=limit))


@router.get("/filings/{symbol}")
def get_filings(symbol: str, limit: int = 8, form_type: str = "") -> dict:
    """SEC EDGAR 报送列表 (免费 provider, form_type 可选过滤如 10-K)。"""
    if limit < 1 or limit > 50:
        return {"available": False, "reason": f"bad_limit: {limit} (1..50)", "source": SOURCE}
    return _respond(symbol, "US",
                    lambda: _provider.filings(symbol, limit=limit, form_type=form_type or None))


@router.get("/summary/{symbol}")
def get_summary(symbol: str, market: str = "US") -> dict:
    """给前端/大模型用的一段盘口摘要文本。"""
    m = _norm_market(market)
    return _respond(symbol, m, lambda: _provider.summary_text(symbol, market=m))
