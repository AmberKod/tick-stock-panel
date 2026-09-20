"""港股 API (M1) — 池 / 基础 / 实时 / 指数 / 日 K。

M1 范围:
- /api/hk/stocks           M1 内置 10 龙头池
- /api/hk/stocks/{symbol}  单只港股基础信息
- /api/hk/indices          港股核心指数 (恒生/恒生科技/恒生中国企业)
- /api/hk/realtime/{sym}   单只实时行情 (H5: quickquote 走腾讯/新浪)
- /api/hk/realtime/batch   批量实时 (M1: 同步逐个拉, M2 改并发)
- /api/hk/daily/{sym}      日 K 拉取 (H6: akshare 落盘, 容错)
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.data_providers.hk_quickquote_provider import (
    batch_quotes_to_df,
    fetch_quote_sync,
    fetch_quotes_batch_sync,
)
from app.services.hk_data_adapter import HK_DEMO_NAMES, HK_DEMO_SYMBOLS, load_demo_instruments

router = APIRouter(prefix="/api/hk", tags=["hk"])


class RealtimeBatchRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list, max_length=10000)


@router.get("/stocks")
def list_hk_stocks() -> dict:
    """港股池: 优先读全量 hk_instruments.parquet, 无则内置 10 龙头。"""
    import polars as pl

    from app.config import settings

    path = settings.data_dir / "instruments" / "hk_instruments.parquet"
    if path.exists():
        try:
            full = pl.read_parquet(path)
            if full.height > 0:
                return {
                    "results": full.to_dicts(),
                    "count": full.height,
                    "currency": "HKD",
                    "settlement": "T+0",
                    "source": "hk_instruments",
                }
        except Exception:
            pass
    df = load_demo_instruments()
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "currency": "HKD",
        "settlement": "T+0",
        "source": "hk_demo",
    }


@router.post("/instruments/sync")
def sync_hk_instruments_endpoint(
    request: Request,
    use_akshare: bool = Query(True, description="akshare 可用时拉全市场"),
    allow_demo: bool = Query(False, description="允许失败时写入 Demo 池，仅用于开发测试"),
) -> dict:
    """同步港股池；默认严格模式，获取不到全量数据时不覆盖正式快照。

    开发测试可显式传 ``allow_demo=true`` 写入内置 10 龙头池。
    """
    from app.config import settings
    from app.services.hk_data_adapter import sync_hk_instruments

    n = sync_hk_instruments(
        settings.data_dir,
        use_akshare=use_akshare,
        allow_demo=allow_demo,
    )
    from app.api.market_data import refresh_market_state

    refresh_market_state(request, "HK")
    return {
        "status": "ok",
        "instruments_written": n,
        "akshare_enabled": use_akshare,
        "demo_allowed": allow_demo,
    }


@router.get("/stocks/{symbol}")
def get_hk_stock(symbol: str) -> dict:
    """单只港股基础信息。"""
    sym = symbol.upper()
    if not sym.endswith(".HK"):
        sym = f"{sym.zfill(5)}.HK"
    if sym not in HK_DEMO_SYMBOLS:
        raise HTTPException(status_code=404, detail=f"HK stock not in M1 demo pool: {sym}")
    return {
        "symbol": sym,
        "name": HK_DEMO_NAMES.get(sym, sym),
        "code": sym.split(".")[0],
        "market": "HK",
        "currency": "HKD",
        "settlement": "T+0",
        "has_price_limit": False,
    }


@router.get("/indices")
def list_hk_indices() -> dict:
    """港股核心指数。"""
    from app.markets import get_profile
    hk = get_profile("HK")
    return {
        "results": [{"symbol": r.symbol, "name": r.name} for r in hk.core_indices],
        "currency": "HKD",
    }


@router.get("/overview")
def get_hk_overview(as_of: date | None = Query(None, description="指定日期 YYYY-MM-DD")) -> dict:
    """港股市场总览 (对齐 A股 /api/overview/market 结构)。

    读 data/kline_hk_us_enriched/symbol=*.HK 预计算指标, 聚合广度/趋势/四榜/
    强势梯队/情绪雷达。无涨停制度, limit 段用强势股(涨幅>=5%)替代。
    """
    from app.config import settings
    from app.services.hk_us_overview_builder import build_hk_us_overview
    return build_hk_us_overview("HK", settings.data_dir, as_of)


# ── H5 实时行情 ──

def _normalize_hk_input(symbol: str) -> str:
    s = symbol.upper().strip()
    if not s.endswith(".HK") and not s.startswith("^"):
        # 5 位纯数字
        if len(s) == 5 and s.isdigit():
            s = f"{s}.HK"
    return s


def _get_hk_realtime_batch(sym_list: list[str]) -> dict:
    if not sym_list:
        return {"results": [], "count": 0, "currency": "HKD"}
    quotes = fetch_quotes_batch_sync(sym_list, timeout=3.0)
    for q in quotes:
        q["market"] = "HK"
    df = batch_quotes_to_df(quotes)
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "requested": len(sym_list),
        "currency": "HKD",
    }


@router.get("/realtime/batch")
def get_hk_realtime_batch(symbols: str = Query(..., description="逗号分隔 symbol")) -> dict:
    """批量港股实时行情 — 腾讯逗号批量优先, 新浪补漏, 并发分片。"""
    sym_list = [_normalize_hk_input(s.strip()) for s in symbols.split(",") if s.strip()]
    return _get_hk_realtime_batch(sym_list)


@router.post("/realtime/batch")
def post_hk_realtime_batch(payload: RealtimeBatchRequest) -> dict:
    """批量港股实时行情 POST 入口，避免超长 URL。"""
    sym_list = [_normalize_hk_input(symbol.strip()) for symbol in payload.symbols if symbol.strip()]
    return _get_hk_realtime_batch(sym_list)


@router.get("/realtime/{symbol}")
def get_hk_realtime(symbol: str) -> dict:
    """单只港股实时行情 — 腾讯 r_hk* 优先, 新浪 hk* 降级。

    网络/解析失败 → 返回 source='unavailable', 不抛错, 前端按"行情暂不可用"处理。
    """
    sym = _normalize_hk_input(symbol)
    quote = fetch_quote_sync(sym, timeout=4.0)
    if quote is None:
        return {
            "symbol": sym,
            "source": "unavailable",
            "message": "行情暂不可用 (网络/解析失败)",
        }
    quote["market"] = "HK"
    return quote


# ── H6 日 K ──

@router.get("/daily/{symbol}")
def get_hk_daily(
    symbol: str,
    request: Request,
    start: date | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end: date | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    days: int = Query(120, ge=10, le=2000),
) -> dict:
    """Read stored bars, or the same normalized provider, without a GET write."""
    from datetime import datetime, time

    import polars as pl

    from app.config import settings
    from app.data_providers.registry import get_default_provider
    from app.markets import get_profile
    from app.services.hk_data_adapter import load_hk_raw_verification_archives, read_hk_daily

    sym = _normalize_hk_input(symbol)
    end_d = end or get_profile("HK").today()
    start_d = start or (end_d - timedelta(days=days))
    if start_d > end_d:
        raise HTTPException(status_code=400, detail="起始日期不能晚于截止日期")
    repo = getattr(request.app.state, "repo", None)
    root = repo.store.data_dir if repo is not None else settings.data_dir
    df = read_hk_daily(sym, root)
    items: list[dict] = []
    if not df.is_empty():
        df = df.filter(pl.col("date").cast(pl.Date).is_between(start_d, end_d))
    if df.is_empty():
        fetched = get_default_provider("HK", dataset="daily").get_daily_with_report(
            [sym], datetime.combine(start_d, time.min), datetime.combine(end_d, time.max), "stock",
            verification_archives=load_hk_raw_verification_archives(root, [sym]),
        )
        df, items = fetched.frame, list(fetched.items)
    if df.is_empty():
        return {
            "symbol": sym,
            "name": HK_DEMO_NAMES.get(sym, ""),
            "rows": [],
            "source": "unavailable",
            "message": "日 K 暂不可用, 请查看数据同步状态",
            "items": items,
        }
    return {
        "symbol": sym,
        "name": HK_DEMO_NAMES.get(sym, ""),
        "rows": df.to_dicts(),
        "source": "+".join(df["source"].drop_nulls().unique().sort().to_list()) if "source" in df.columns else "legacy_unknown",
        "price_adjustment": df["price_adjustment"][0] if "price_adjustment" in df.columns else "unknown",
        "items": items,
        "start": str(start_d),
        "end": str(end_d),
    }


# ── U3: 港股 enriched 落盘 (让 Screener/Monitor/回测能筛港股) ──

@router.post("/enriched/sync")
def sync_hk_enriched(request: Request, symbols: str | None = Query(None, description="逗号分隔; 不传则重算全部港股")) -> dict:
    """把港股日 K 算成 enriched 落盘 (供 Screener/Monitor/回测复用)。

    一次性调用: 把 10 龙头 (或全市场) 的 enriched 落盘。
    后续 Screener 用 pool=['00700.HK', ...] 就能筛港股。
    """
    from app.config import settings
    from app.services.market_data_status import recompute_market_enriched

    try:
        selected = None if symbols is None else [symbol.strip() for symbol in symbols.split(",") if symbol.strip()]
        result = recompute_market_enriched(settings.data_dir, "HK", selected)
        from app.api.market_data import refresh_market_state

        refresh_market_state(request, "HK")
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/data/status")
def get_hk_data_status(request: Request) -> dict:
    from app.config import settings
    from app.services.market_data_status import get_market_data_status

    return get_market_data_status(settings.data_dir, "HK", getattr(request.app.state, "capabilities", None))


@router.post("/daily/sync")
async def sync_hk_daily(
    request: Request,
    symbols: str | None = Query(None),
    start: date | None = None,
    end: date | None = None,
) -> dict:
    from app.api.market_data import start_market_download

    return await start_market_download(request, "HK", symbols, start, end)


@router.post("/instruments/lot-sizes/sync")
def sync_hk_instrument_lots(request: Request) -> dict:
    from app.config import settings
    from app.services.hk_data_adapter import sync_hk_lot_sizes

    try:
        result = sync_hk_lot_sizes(settings.data_dir)
        from app.api.market_data import refresh_market_state

        refresh_market_state(request, "HK")
        return result
    except Exception:
        raise HTTPException(status_code=502, detail="港交所每手数据暂不可用,已保留现有标的池") from None


@router.post("/financials/sync")
async def sync_hk_financials(request: Request, symbols: str | None = Query(None)) -> dict:
    from app.api.market_data import start_hk_financial_sync

    return await start_hk_financial_sync(request, symbols)
