"""美股深度数据 API — OpenBB (九章融合 P1)。

- GET /api/us/deep/quote/{symbol}: 实时盘口 (bid/ask/均线/年内高低)
- GET /api/us/deep/statement/{symbol}: 三大报表 (income|balance|cash)
- GET /api/us/deep/filings/{symbol}: SEC 报送列表
- GET /api/us/deep/summary/{symbol}: 盘口摘要文本
- GET /api/us/deep/institutional/{symbol}: 机构持股(谁在增减持)
- GET /api/us/deep/insider/{symbol}: 内部人交易(Form 4 明细)
- GET /api/us/deep/estimates/{symbol}: 分析师一致预期(目标价/评级/覆盖数)

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


def _respond(symbol: str, market: str, call, *, empty_if=None,
             no_data_reason: str = "no_data: OpenBB 无该标的数据") -> dict:
    """统一响应: 成功过闸门 → available:true; 异常/无数据/被拦 → HTTP 200 + available:false。

    ``call`` 是 ``() -> {"source": ..., "data": ...}`` 的 provider 调用。

    ``empty_if`` 给"**有壳无值**"的端点用: 有些 provider 即使没取到数也返回
    一个只有 symbol 的 dict(不是空 dict), 光看 ``not data`` 判不出来, 会让
    一排 None 流向前端冒充成功。传 ``empty_if`` 显式说明"什么算没数据"。
    """
    try:
        payload = call()
    except Exception as exc:
        logger.warning("us deep provider error (%s %s): %s", symbol, market, exc)
        return {"available": False, "reason": f"provider_error: {exc}", "source": SOURCE}

    data = payload.get("data")
    empty = not data or data == {} or data == []
    if not empty and empty_if is not None:
        empty = bool(empty_if(data))
    if empty:
        return {"available": False, "reason": no_data_reason, "source": SOURCE}

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


@router.get("/institutional/{symbol}")
def get_institutional(symbol: str, limit: int = 20) -> dict:
    """机构持股 —— 谁持有、增持还是减持(对标 A股龙虎榜的"机构动向"位)。

    limit 1..100 默认 20。走 nasdaq provider(免费); **不用** OpenBB 的
    ``/ownership/form_13f`` —— 那个端点只收 provider=sec, 本机取不到数。

    ⚠️ 源站的 ``market_value`` 单位存疑(疑似千美元口径), 故不做换算也不
    展示, 字段以 ``_raw`` 结尾留给核对; 判断机构动向看持股数与变动%即可。
    """
    if limit < 1 or limit > 100:
        return {"available": False, "reason": f"bad_limit: {limit} (1..100)", "source": SOURCE}
    return _respond(symbol, "US", lambda: _provider.institutional(symbol, limit=limit),
                    no_data_reason="no_data: 无机构持股披露(该标的可能未被机构覆盖)")


@router.get("/insider/{symbol}")
def get_insider(symbol: str, limit: int = 20) -> dict:
    """内部人交易(Form 4 明细) —— 高管/董事买卖股数、成交价、交易后持股。

    limit 1..100 默认 20。与 ``/filings`` 的区别: filings 是**报送**列表(只有
    报送类型与日期), 这里是**交易本身**(类型 Automatic Sell / Award ...、
    股数、价格、持有人身份 Officer / Director)。
    """
    if limit < 1 or limit > 100:
        return {"available": False, "reason": f"bad_limit: {limit} (1..100)", "source": SOURCE}
    return _respond(symbol, "US", lambda: _provider.insider_trading(symbol, limit=limit),
                    no_data_reason="no_data: 无内部人交易记录")


@router.get("/estimates/{symbol}")
def get_estimates(symbol: str) -> dict:
    """分析师一致预期 —— 目标价高/低/均值/中位数 + 评级 + 覆盖机构数。

    美股侧对标 A股「研报」位的数据, 走 yfinance provider。
    ``recommendation``(buy/hold/...)与 ``number_of_analysts`` 都是真实汇总。

    ⚠️ yfinance 对无覆盖标的会返回**只有 symbol 的壳**(不是空 dict), 光判
    ``not data`` 抓不到 → 用 empty_if 显式判定"核心值全 None 即无数据"。
    """
    def _empty(d: dict) -> bool:
        return d.get("target_consensus") is None and d.get("number_of_analysts") is None

    return _respond(symbol, "US", lambda: _provider.estimates(symbol), empty_if=_empty,
                    no_data_reason="no_data: 无分析师覆盖(yfinance 未给出目标价与覆盖机构数)")
