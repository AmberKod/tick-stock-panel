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

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from app.services.hk_data_adapter import HK_DEMO_NAMES, HK_DEMO_SYMBOLS, load_demo_instruments
from app.data_providers.hk_quickquote_provider import (
    batch_quotes_to_df,
    fetch_quote_sync,
)

router = APIRouter(prefix="/api/hk", tags=["hk"])


@router.get("/stocks")
def list_hk_stocks() -> dict:
    """M1 内置 10 个港股龙头。"""
    df = load_demo_instruments()
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "currency": "HKD",
        "settlement": "T+0",
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


# ── H5 实时行情 ──

def _normalize_hk_input(symbol: str) -> str:
    s = symbol.upper().strip()
    if not s.endswith(".HK") and not s.startswith("^"):
        # 5 位纯数字
        if len(s) == 5 and s.isdigit():
            s = f"{s}.HK"
    return s


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


@router.get("/realtime/batch")
def get_hk_realtime_batch(symbols: str = Query(..., description="逗号分隔 symbol")) -> dict:
    """批量港股实时行情。

    symbols=00700.HK,09988.HK,HSI.HK
    """
    sym_list = [_normalize_hk_input(s.strip()) for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return {"results": [], "count": 0, "currency": "HKD"}
    quotes = []
    for s in sym_list:
        q = fetch_quote_sync(s, timeout=3.0)
        if q is not None:
            q["market"] = "HK"
            quotes.append(q)
    df = batch_quotes_to_df(quotes)
    return {
        "results": df.to_dicts(),
        "count": df.height,
        "requested": len(sym_list),
        "currency": "HKD",
    }


# ── H6 日 K ──

@router.get("/daily/{symbol}")
def get_hk_daily(
    symbol: str,
    start: date | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end: date | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
    days: int = Query(120, ge=10, le=2000),
) -> dict:
    """单只港股日 K (H6 akshare)。

    akshare 不可用 → 返回空 rows + source='unavailable'。
    数据写入 data/kline_daily/hk/{symbol}.parquet 供二次读取加速。
    """
    from app.services.hk_data_adapter import fetch_hk_daily_akshare, sync_hk_daily_to_parquet
    sym = _normalize_hk_input(symbol)
    end_d = end or date.today()
    start_d = start or (end_d.toordinal() - days)
    df = fetch_hk_daily_akshare(sym, start_d, end_d)
    if df.is_empty():
        return {
            "symbol": sym,
            "name": HK_DEMO_NAMES.get(sym, ""),
            "rows": [],
            "source": "unavailable",
            "message": "日 K 暂不可用 (akshare 未装 或 网络失败)",
        }
    # 写盘 (容错)
    sync_hk_daily_to_parquet(df, sym)
    return {
        "symbol": sym,
        "name": HK_DEMO_NAMES.get(sym, ""),
        "rows": df.to_dicts(),
        "source": "akshare",
        "start": str(start_d),
        "end": str(end_d),
    }


# ── U3: 港股 enriched 落盘 (让 Screener/Monitor/回测能筛港股) ──

@router.post("/enriched/sync")
def sync_hk_enriched(symbols: str | None = Query(None, description="逗号分隔; None=扫描全部")) -> dict:
    """把港股日 K 算成 enriched 落盘 (供 Screener/Monitor/回测复用)。

    一次性调用: 把 10 龙头 (或全市场) 的 enriched 落盘。
    后续 Screener 用 pool=['00700.HK', ...] 就能筛港股。
    """
    from app.services.hk_data_adapter import sync_all_hk_daily_to_enriched
    sym_list = None
    if symbols:
        sym_list = [_normalize_hk_input(s) for s in symbols.split(",") if s.strip()]
    written = sync_all_hk_daily_to_enriched(sym_list)
    return {
        "status": "ok",
        "enriched_dates_written": written,
        "symbols": sym_list or "auto-scan",
    }
