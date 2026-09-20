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

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.data_providers.market_provider_factory import create_market_realtime_provider
from app.data_providers.yfinance_provider import (
    US_DEMO_NAMES,
    YFinanceProvider,
)

router = APIRouter(prefix="/api/us", tags=["us"])


class RealtimeBatchRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=10000)


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
    """美股池: 优先读全量 us_instruments.parquet, 无则内置 15 龙头。"""
    import polars as pl

    from app.config import settings

    path = settings.data_dir / "instruments" / "us_instruments.parquet"
    if path.exists():
        try:
            full = pl.read_parquet(path)
            if full.height > 0:
                return {
                    "results": full.to_dicts(),
                    "count": full.height,
                    "currency": "USD",
                    "currency_label": "$",
                    "realtime_delay_min": 15,
                    "source": "us_instruments",
                }
        except Exception:
            pass
    provider = YFinanceProvider()
    df = provider.get_instruments("stock")
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "currency": "USD",
        "currency_label": "$",
        "realtime_delay_min": 15,
        "source": "us_demo",
    }


@router.post("/instruments/sync")
def sync_us_instruments_endpoint(
    use_akshare: bool = Query(True, description="akshare 可用时拉全市场"),
    allow_demo: bool = Query(False, description="允许失败时写入 Demo 池，仅用于开发测试"),
) -> dict:
    """同步美股池；默认严格模式，获取不到全量数据时不覆盖正式快照。

    开发测试可显式传 ``allow_demo=true`` 写入内置 15 龙头池。
    """
    from app.config import settings
    from app.services.hk_data_adapter import sync_us_instruments

    n = sync_us_instruments(
        settings.data_dir,
        use_akshare=use_akshare,
        allow_demo=allow_demo,
    )
    return {
        "status": "ok",
        "instruments_written": n,
        "akshare_enabled": use_akshare,
        "demo_allowed": allow_demo,
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


@router.get("/overview")
def get_us_overview(as_of: date | None = Query(None, description="指定日期 YYYY-MM-DD")) -> dict:
    """美股市场总览 (对齐 A股 /api/overview/market 结构)。

    读 data/kline_hk_us_enriched/symbol=*.US 预计算指标, 聚合广度/趋势/四榜/
    强势梯队/情绪雷达。无涨停制度, limit 段用强势股(涨幅>=5%)替代。
    """
    from app.config import settings
    from app.services.hk_us_overview_builder import build_hk_us_overview
    return build_hk_us_overview("US", settings.data_dir, as_of)


def _get_us_realtime_batch(sym_list: list[str]) -> dict:
    if not sym_list:
        return {"results": [], "count": 0, "currency": "USD"}
    provider = create_market_realtime_provider("us")
    df = provider.get_realtime(symbols=sym_list)
    df = df.with_columns(__import__("polars").lit("US").alias("market")) if not df.is_empty() else df
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "requested": len(sym_list),
        "currency": "USD",
    }


@router.get("/realtime/batch")
def get_us_realtime_batch(symbols: str = Query(..., description="逗号分隔")) -> dict:
    """批量美股实时行情。"""
    sym_list = [_norm(s.strip()) for s in symbols.split(",") if s.strip()]
    return _get_us_realtime_batch(sym_list)


@router.post("/realtime/batch")
def post_us_realtime_batch(payload: RealtimeBatchRequest) -> dict:
    """批量美股实时行情 POST 入口，避免超长 URL。"""
    sym_list = [_norm(symbol.strip()) for symbol in payload.symbols if symbol.strip()]
    return _get_us_realtime_batch(sym_list)


@router.get("/realtime/{symbol}")
def get_us_realtime(symbol: str) -> dict:
    """单只美股实时 (yfinance fast_info, 延迟 15min)。"""
    sym = _norm(symbol)
    provider = create_market_realtime_provider("us")
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
    from datetime import datetime, time
    df = provider.get_daily(
        [sym], start_time=datetime.combine(start_d, time.min),
        end_time=datetime.combine(end_d, time.max), asset_type="stock",
    )
    if df.is_empty():
        return {
            "symbol": sym,
            "name": US_DEMO_NAMES.get(sym, ""),
            "rows": [],
            "source": "unavailable",
            "message": "日 K 暂不可用 (yfinance 未装 或 网络失败)",
        }
    # 落盘 (复用 HK 同款 sync_hk_daily_to_parquet, 分区 symbol=X.US 同格式)
    df = df.filter((__import__("polars").col("date") >= start_d) & (__import__("polars").col("date") <= end_d))
    sync_hk_daily_to_parquet(df, sym)
    return {
        "symbol": sym,
        "name": US_DEMO_NAMES.get(sym, ""),
        "rows": df.to_dicts(),
        "source": "yfinance",
        "start": str(start_d),
        "end": str(end_d),
    }


# ── U3 同款: 美股 enriched 落盘 (让 Screener/Monitor/回测能筛美股) ──

@router.post("/enriched/sync")
def sync_us_enriched(symbols: str | None = Query(None, description="逗号分隔; None=扫描全部 .US 分区")) -> dict:
    """把美股日 K 算成 enriched 落盘 (供 Screener/Monitor/回测复用)。

    复用 hk_data_adapter.sync_all_hk_daily_to_enriched —— 显式传 US symbols
    时按 .US 分区读写, 与港股同格式 (symbol=X.US)。
    """
    from app.config import settings
    from app.services.market_data_status import recompute_market_enriched

    try:
        selected = None if symbols is None else [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]
        return recompute_market_enriched(settings.data_dir, "US", selected)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data/status")
def get_us_data_status(request: Request) -> dict:
    from app.config import settings
    from app.services.market_data_status import get_market_data_status

    return get_market_data_status(settings.data_dir, "US", getattr(request.app.state, "capabilities", None))


@router.post("/daily/sync")
async def sync_us_daily(
    request: Request,
    symbols: str | None = Query(None),
    start: date | None = None,
    end: date | None = None,
) -> dict:
    from app.api.market_data import start_market_download

    return await start_market_download(request, "US", symbols, start, end)
