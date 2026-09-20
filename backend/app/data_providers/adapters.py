"""Pure adapters for normalizing third-party multi-market quote rows.

External sources use different names, units, and percentage conventions. This
module keeps that conversion explicit and side-effect free so providers can be
added without leaking source-specific payloads into services or the frontend.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

Market = Literal["cn", "hk", "us"]

_NUMERIC_FIELDS = (
    "last_price", "prev_close", "open", "high", "low", "volume", "amount",
    "change_pct",
)


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp_ms(value: Any) -> int | None:
    """Normalize datetime/seconds/milliseconds to Unix milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        except (TypeError, ValueError):
            return None
    # Unix seconds are currently around 1e9; milliseconds around 1e12.
    return int(numeric * 1000) if abs(numeric) < 10_000_000_000 else int(numeric)


def normalize_quote_row(
    row: dict[str, Any],
    *,
    market: Market,
    source: str,
    pct_unit: Literal["fraction", "percent"] = "fraction",
    amount_unit: Literal["yuan", "ten_thousand"] = "yuan",
    volume_unit: Literal["shares", "hands"] = "shares",
) -> dict[str, Any] | None:
    """Map one external quote row to the internal realtime contract.

    The returned percentage is percent-based (1.5 means 1.5%), amount is yuan,
    volume is shares, and timestamp is Unix milliseconds. Rows without a symbol
    are discarded; missing optional quote fields remain ``None``.

    ``pct_unit`` describes the *input* convention: ``"percent"`` means the
    source already uses percent (left unchanged), ``"fraction"`` means the
    source uses a fraction (multiplied by 100). This matches the HK quickquote
    and US yfinance providers, which expose percent-based change_pct to the
    frontend.
    """
    symbol = row.get("symbol") or row.get("code") or row.get("ticker")
    if not symbol:
        return None

    out: dict[str, Any] = {
        "symbol": str(symbol).strip().upper(),
        "name": row.get("name") or row.get("title") or str(symbol),
        "source": source,
        "market": market,
        "last_price": _number(row.get("last_price", row.get("price", row.get("close")))),
        "prev_close": _number(row.get("prev_close", row.get("pre_close", row.get("previous_close")))),
        "open": _number(row.get("open")),
        "high": _number(row.get("high", row.get("highest"))),
        "low": _number(row.get("low", row.get("lowest"))),
        "volume": _number(row.get("volume", row.get("vol"))),
        "amount": _number(row.get("amount", row.get("turnover"))),
        "change_pct": _number(row.get("change_pct", row.get("pct", row.get("change")))),
        "timestamp": _timestamp_ms(row.get("timestamp", row.get("quote_ts", row.get("ts")))),
    }

    if out["change_pct"] is not None and pct_unit == "fraction":
        # 输入小数制 → 百分制 (与港股 quickquote / 美股 yfinance 输出一致)
        out["change_pct"] *= 100
    if amount_unit == "ten_thousand" and out["amount"] is not None:
        out["amount"] *= 10_000
    if volume_unit == "hands" and out["volume"] is not None:
        # One hand is the conventional 100 shares used by the current HK
        # provider; sources with a different lot size must pre-convert.
        out["volume"] *= 100

    return out


def normalize_quote_rows(rows: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Normalize a batch and drop rows that have no usable symbol."""
    return [normalized for row in rows if (normalized := normalize_quote_row(row, **kwargs))]
