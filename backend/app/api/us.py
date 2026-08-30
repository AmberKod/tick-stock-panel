"""美股 API (M2) — 热门池 / 基础 / 实时 / 指数 / 日 K。

范围:
- /api/us/stocks            M2 内置 15 热门龙头
- /api/us/stocks/{symbol}   单只美股基础信息
- /api/us/indices           三大指数 (标普/纳指/道琼斯)
- /api/us/realtime/{sym}    实时 (yfinance fast_info, 延迟 15min)
- /api/us/daily/{sym}       日 K (yfinance, 落盘 kline_daily/symbol=X.US/)
- /api/us/realtime/batch    批量实时
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.data_providers.yfinance_provider import (
    US_DEMO_NAMES,
    US_DEMO_SYMBOLS,
    YFinanceProvider,
)

router = APIRouter(prefix="/api/us", tags=["us"])


def _norm(symbol: str) -> str:
    """AAPL → AAPL.US, ^GSPC → ^GSPC.US, AAPL.US → AAPL.US。"""
    s = symbol.upper().strip()
    if s.endswith(".US"):
        return s
    if s.startswith("^"):
        return f"{s}.US"
    return f"{s}.US"


@router.get("/stocks")
def list_us_stocks() -> dict:
    """M2 内置 15 热门美股 (S&P 头部 + 科技七姐妹 + 中概)。"""
    provider = YFinanceProvider()
    df = provider.get_instruments("stock")
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "currency": "USD",
        "currency_label": "$",
        "realtime_delay_min": 15,
    }


@router.get("/stocks/{symbol}")
def get_us_stock(symbol: str) -> dict:
    """单只美股基础信息。"""
    sym = _norm(symbol)
    return {
        "symbol": sym,
        "name": US_DEMO_NAMES.get(sym, sym),
        "code": sym.split(".")[0],
        "market": "US",
        "currency": "USD",
        "currency_label": "$",
        "settlement": "T+0",
        "has_price_limit": False,
        "realtime_delay_min": 15,
    }


@router.get("/indices")
def list_us_indices() -> dict:
    """三大指数 (标普/纳指/道琼斯)。"""
    from app.markets import get_profile
    us = get_profile("US")
    return {
        "results": [{"symbol": r.symbol, "name": r.name} for r in us.core_indices],
        "currency": "USD",
        "currency_label": "$",
    }


@router.get("/realtime/{symbol}")
def get_us_realtime(symbol: str) -> dict:
    """单只美股实时 (yfinance fast_info, 延迟 15min)。"""
    sym = _norm(symbol)
    provider = YFinanceProvider()
    df = provider.get_realtime(symbols=[sym])
    if df.is_empty():
        return {
            "symbol": sym,
            "source": "unavailable",
            "message": "行情暂不可用 (yfinance 未装 或 网络失败)",
        }
    row = df.to_dicts()[0]
    row["market"] = "US"
    return row


@router.get("/realtime/batch")
def get_us_realtime_batch(symbols: str = Query(..., description="逗号分隔")) -> dict:
    """批量美股实时。"""
    sym_list = [_norm(s.strip()) for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return {"results": [], "count": 0, "currency": "USD"}
    provider = YFinanceProvider()
    df = provider.get_realtime(symbols=sym_list)
    df = df.with_columns(__import__("polars").lit("US").alias("market")) if not df.is_empty() else df
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "requested": len(sym_list),
        "currency": "USD",
    }


@router.get("/daily/{symbol}")
def get_us_daily(
    symbol: str,
    start: date | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end: date | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    days: int = Query(120, ge=10, le=2000),
) -> dict:
    """单只美股日 K (yfinance, 落盘 kline_daily/symbol=X.US/)。"""
    from datetime import timedelta
    from app.services.hk_data_adapter import sync_hk_daily_to_parquet
    sym = _norm(symbol)
    end_d = end or date.today()
    start_d = start or (end_d - timedelta(days=days))
    provider = YFinanceProvider()
    df = provider.get_daily([sym], start_time=start_d, end_time=None)
    if df.is_empty():
        return {
            "symbol": sym,
            "name": US_DEMO_NAMES.get(sym, ""),
            "rows": [],
            "source": "unavailable",
            "message": "日 K 暂不可用 (yfinance 未装 或 网络失败)",
        }
    # 落盘 (复用 HK 同款 sync_hk_daily_to_parquet, 分区 symbol=X.US 同格式)
    df = df.filter(__import__("polars").col("date") >= start_d)
    sync_hk_daily_to_parquet(df, sym)
    return {
        "symbol": sym,
        "name": US_DEMO_NAMES.get(sym, ""),
        "rows": df.to_dicts(),
        "source": "yfinance",
        "start": str(start_d),
        "end": str(end_d),
    }