"""Normalize provider responses into internal Polars schemas."""
from __future__ import annotations

import math
import re

import polars as pl

from app.indicators.pipeline import filter_halt_days
from app.markets.registry import resolve_market

DAILY_COLS = [
    "symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts",
    "source", "currency", "volume_unit", "amount_source", "price_adjustment",
    "price_schema_version", "raw_price_verified", "observed_at",
    "adjustment_source", "adjustment_version", "adjustment_as_of", "verification_source",
]
ADJ_FACTOR_COLS = ["symbol", "trade_date", "ex_factor"]
INSTRUMENT_COLS = [
    "symbol", "name", "code", "exchange", "asset_type", "source", "market", "sector", "industry",
    "lot_size", "lot_size_source", "lot_size_as_of",
    "currency", "lot_size_observed_at", "lot_size_effective_from", "lot_size_status",
]


def normalize_lot_size(value: object) -> int | None:
    """Accept an explicit positive integer board lot; never infer it from a ticker."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (ValueError, TypeError):
        return None
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return int(number) if number <= 2**63 - 1 else None


def normalize_market_symbols(symbols: list[str], market: str) -> list[str]:
    """Validate a single HK/US stock pool, preserving class-share dots."""
    market = str(market).upper()
    if market not in {"HK", "US"}:
        raise ValueError("市场只支持 HK 或 US")
    result: list[str] = []
    for value in symbols:
        symbol = str(value or "").strip().upper()
        suffix = f".{market}"
        if symbol.endswith(suffix):
            code = symbol[:-3]
        else:
            code = symbol
            if symbol.endswith((".HK", ".US", ".SH", ".SZ", ".BJ", ".SS")):
                raise ValueError(f"标的 {symbol} 不属于 {market} 市场")
        if market == "HK":
            if not re.fullmatch(r"\d{1,5}", code):
                raise ValueError(f"无效的港股代码: {symbol}")
            normalized = f"{code.zfill(5)}.HK"
        else:
            if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", code):
                raise ValueError(f"无效的美股代码: {symbol}")
            normalized = f"{code}.US"
        if normalized not in result:
            result.append(normalized)
    return result


def to_polars(data) -> pl.DataFrame:
    if data is None:
        return pl.DataFrame()
    if isinstance(data, pl.DataFrame):
        return data
    if isinstance(data, dict):
        rows: list[dict] = []
        for sym, values in data.items():
            for item in values or []:
                row = dict(item or {})
                row.setdefault("symbol", sym)
                rows.append(row)
        return pl.DataFrame(rows) if rows else pl.DataFrame()
    if hasattr(data, "reset_index"):
        return pl.from_pandas(data.reset_index())
    try:
        return pl.DataFrame(data)
    except Exception:
        return pl.DataFrame()


def normalize_daily(data, default_symbol: str | None = None, source: str = "tickflow") -> pl.DataFrame:
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "ts_code": "symbol",
        "trade_date": "date",
        "datetime": "date",
        "vol": "volume",
        "amt": "amount",
        "timestamp": "quote_ts",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "symbol" not in df.columns and default_symbol:
        df = df.with_columns(pl.lit(default_symbol).alias("symbol"))
    if "date" in df.columns and df.schema["date"] != pl.Date:
        df = df.with_columns(pl.col("date").cast(pl.Date, strict=False))
    # quote_ts: 毫秒级行情时间戳, 用于盘后校验/量比折算。保留为 Int64, 缺失则置 null。
    if "quote_ts" in df.columns:
        df = df.with_columns(pl.col("quote_ts").cast(pl.Int64, strict=False))
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df = df.with_columns(pl.col(col).cast(pl.Float64, strict=False))
    df = filter_halt_days(df)
    keep = [c for c in DAILY_COLS if c in df.columns]
    return df.select(keep) if keep else pl.DataFrame()


def normalize_adj_factors(data, source: str = "tickflow") -> pl.DataFrame:
    df = to_polars(data)
    if df.is_empty():
        return df
    rename_map = {
        "timestamp": "trade_date",
        "date": "trade_date",
        "adj_factor": "ex_factor",
    }
    df = df.rename({k: v for k, v in rename_map.items() if k in df.columns})
    if "trade_date" in df.columns:
        if df.schema["trade_date"] in {pl.Int64, pl.Int32, pl.UInt64, pl.UInt32, pl.Float64, pl.Float32}:
            df = df.with_columns(
                pl.from_epoch(pl.col("trade_date").cast(pl.Int64), time_unit="ms").dt.date().alias("trade_date")
            )
        else:
            df = df.with_columns(pl.col("trade_date").cast(pl.Date, strict=False))
    if "ex_factor" in df.columns:
        df = df.with_columns(pl.col("ex_factor").cast(pl.Float64, strict=False))
    keep = [c for c in ADJ_FACTOR_COLS if c in df.columns]
    return df.select(keep).drop_nulls() if len(keep) == len(ADJ_FACTOR_COLS) else pl.DataFrame()


def normalize_instruments(rows: list[dict], asset_type: str, source: str = "tickflow") -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    out: list[dict] = []
    for item in rows:
        symbol = item.get("symbol")
        if not symbol:
            continue
        out.append({
            "symbol": str(symbol),
            "name": item.get("name") or str(symbol),
            "code": item.get("code") or str(symbol).rsplit(".", 1)[0],
            "exchange": item.get("exchange"),
            "asset_type": asset_type,
            "source": source,
            "sector": item.get("sector") or None,
            "industry": item.get("industry") or None,
            "lot_size": normalize_lot_size(item.get("lot_size")),
            "lot_size_source": item.get("lot_size_source") or None,
            "lot_size_as_of": str(item["lot_size_as_of"]) if item.get("lot_size_as_of") else None,
            "currency": ("CNY" if str(item.get("currency") or "").upper() == "RMB"
                         else str(item.get("currency") or "").upper() or None),
            "lot_size_observed_at": item.get("lot_size_observed_at") or None,
            "lot_size_effective_from": str(item["lot_size_effective_from"]) if item.get("lot_size_effective_from") else None,
            "lot_size_status": item.get("lot_size_status") or None,
        })
    if not out:
        return pl.DataFrame()
    return (
        pl.DataFrame(out)
        .with_columns(
            pl.col("symbol").map_elements(resolve_market, return_dtype=pl.Utf8).alias("market"),
            pl.col("lot_size").cast(pl.Int64),
            pl.col("lot_size_source").cast(pl.String),
            pl.col("lot_size_as_of").cast(pl.Date, strict=False),
            pl.col("currency").cast(pl.String),
            pl.col("lot_size_observed_at").cast(pl.String),
            pl.col("lot_size_effective_from").cast(pl.Date, strict=False),
            pl.col("lot_size_status").cast(pl.String),
        )
        .select(INSTRUMENT_COLS)
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )
