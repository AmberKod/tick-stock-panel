"""Explicit board-lot metadata from HKEX's public List of Securities."""
from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime

import polars as pl

from app.data_providers.normalizer import normalize_lot_size
from app.markets.hk import HK_TZ

HKEX_SECURITIES_URL = (
    "https://www.hkex.com.hk/eng/services/trading/securities/"
    "securitieslists/ListOfSecurities.xlsx"
)


def parse_hkex_lot_sizes(content: bytes) -> pl.DataFrame:
    """Keep every currency and conflicting observation without inventing lots."""
    table = pl.read_excel(
        io.BytesIO(content), engine="calamine", has_header=False,
        drop_empty_rows=False, drop_empty_cols=False, infer_schema_length=None,
    )
    header_row: int | None = None
    as_of: str | None = None
    for index, row in enumerate(table.head(10).iter_rows()):
        values = [str(value).strip() for value in row]
        if "Stock Code" in values and "Board Lot" in values:
            header_row = int(index)
        for value in values:
            match = re.search(r"Updated as at (\d{2}/\d{2}/\d{4})", value)
            if match:
                as_of = datetime.strptime(match.group(1), "%d/%m/%Y").date()
    if header_row is None or as_of is None:
        raise ValueError("港交所证券清单缺少 Stock Code / Board Lot 或更新日期")
    table.columns = [str(value).strip() if value is not None else f"_unused_{index}"
                     for index, value in enumerate(table.row(header_row))]
    required = {"Stock Code", "Board Lot", "Trading Currency"}
    if not required.issubset(table.columns):
        raise ValueError("港交所证券清单缺少币种字段")
    rows: list[dict] = []
    observed_at = datetime.now(UTC).isoformat()
    today = datetime.now(HK_TZ).date()
    for row in table.slice(header_row + 1).iter_rows(named=True):
        code = str(row.get("Stock Code", "")).strip()
        lot = normalize_lot_size(row.get("Board Lot"))
        if not re.fullmatch(r"\d{1,5}", code):
            continue
        currency = str(row.get("Trading Currency", "")).strip().upper()
        currency = "CNY" if currency == "RMB" else currency
        currency = currency if currency in {"HKD", "CNY", "USD"} else None
        rows.append({
            "symbol": f"{code.zfill(5)}.HK",
            "lot_size": lot,
            "lot_size_source": "hkex_list_of_securities",
            "lot_size_as_of": as_of,
            "currency": currency,
            "lot_size_observed_at": observed_at,
            "lot_size_effective_from": None,
            "lot_size_status": "missing" if lot is None or currency is None else "future_snapshot" if as_of > today else "verified_snapshot",
        })
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(row)
    normalized: list[dict] = []
    for candidates in grouped.values():
        row = candidates[0].copy()
        if len({(value["lot_size"], value["currency"]) for value in candidates}) > 1:
            row.update(lot_size=None, currency=None, lot_size_status="conflict",
                       lot_size_candidates=json.dumps(candidates, ensure_ascii=False, default=str))
        else:
            row["lot_size_candidates"] = None
        normalized.append(row)
    return pl.DataFrame(normalized).with_columns(
        pl.col("lot_size").cast(pl.Int64), pl.col("currency").cast(pl.String),
        pl.col("lot_size_as_of").cast(pl.Date), pl.col("lot_size_effective_from").cast(pl.Date),
        pl.col("lot_size_candidates").cast(pl.String),
    ).sort("symbol") if normalized else pl.DataFrame()


def fetch_hkex_lot_sizes() -> pl.DataFrame:
    """Fetch metadata without changing the user's instrument or price snapshots."""
    import httpx

    with httpx.stream("GET", HKEX_SECURITIES_URL, timeout=20, follow_redirects=True) as response:
        response.raise_for_status()
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 8 * 1024 * 1024:
                raise ValueError("港交所证券清单超过大小限制")
            chunks.append(chunk)
    frame = parse_hkex_lot_sizes(b"".join(chunks))
    if frame.is_empty():
        raise ValueError("港交所证券清单没有可用的证券每手数据")
    return frame
