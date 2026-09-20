"""腾讯财经港美股候选 Provider。

This is an isolated compatibility provider based on the allstock-data
reference. It is intentionally not registered as a default source yet:
production adoption still requires live stability, rate-limit, unit and
market-session verification.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
import polars as pl

from app.data_providers.adapters import normalize_quote_row
from app.data_providers.base import AssetType, ProviderCapabilities
from app.data_providers.normalizer import DAILY_COLS

Market = Literal["hk", "us"]
_QUOTE_RE = re.compile(r'="([^"]*)"')
_LINE_RE = re.compile(r'v_(\w+)="([^"]*)"')


def _market_code(symbol: str, market: Market) -> str:
    raw = str(symbol or "").strip().upper()
    code = raw.split(".", 1)[0]
    if market == "hk":
        return f"hk{code.zfill(5)}"
    return f"us{code}"


def _key_to_symbol(key: str, market: Market) -> str | None:
    """腾讯返回 key (usAAPL / hk00700 / r_hk00700) → 内部 symbol (AAPL.US / 00700.HK)。"""
    k = str(key or "").strip().upper()
    if market == "us":
        code = k.removeprefix("US")
        return f"{code}.US" if code else None
    code = k.removeprefix("R_HK").removeprefix("HK")
    if code.isdigit() and len(code) == 5:
        return f"{code}.HK"
    return None


def parse_tencent_quote(
    payload: str,
    *,
    symbol: str,
    market: Market,
    source: str = "tencent_allstock_compat",
) -> dict[str, Any] | None:
    """Parse one qt.gtimg.cn response into the internal quote contract."""
    match = _QUOTE_RE.search(payload or "")
    if not match:
        return None
    fields = match.group(1).split("~")
    if len(fields) < 33:
        return None

    def value(index: int) -> Any:
        return fields[index] if index < len(fields) else None

    # Tencent quote layout used by the allstock-data reference and existing HK
    # provider: price/pre-close/open/volume at 3/4/5/6, pct at 32.
    raw = {
        "symbol": symbol,
        "name": value(1),
        "price": value(3),
        "pre_close": value(4),
        "open": value(5),
        "volume": value(6),
        "amount": value(37),
        "high": value(33),
        "low": value(34),
        "pct": value(32),
        # Tencent field 30 is the source quote time. It is a local exchange
        # timestamp: Hong Kong time for HK and New York time for US.
        "quote_ts": value(30),
    }
    if market == "hk":
        raw["timestamp"] = _source_timestamp_ms(raw["quote_ts"], "Asia/Hong_Kong")
    else:
        raw["timestamp"] = _source_timestamp_ms(raw["quote_ts"], "America/New_York")
    return normalize_quote_row(
        raw,
        market=market,
        source=source,
        pct_unit="percent",
        # Field 37 is observed as currency units for both markets. Keep the
        # source value unchanged until a separate source specification proves
        # a different HK unit; multiplying it would inflate live turnover.
        amount_unit="yuan",
        # Live HK and US samples both expose field 6 in shares.
        volume_unit="shares",
    )


def parse_tencent_kline(
    payload: str,
    *,
    symbol: str,
    source: str = "tencent_allstock_compat",
) -> pl.DataFrame:
    """Parse documented fqkline JSON rows: date/open/close/high/low/volume."""
    try:
        body = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return pl.DataFrame()
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, dict):
        return pl.DataFrame()
    # Tencent keys are usually e.g. "hk00700" or "usAAPL". Pick the first
    # nested list because the request contains exactly one symbol.
    values: Any = None
    selected_series: str | None = None
    for item in rows.values():
        if isinstance(item, dict):
            selected_series = next((key for key in ("qfqday", "day", "qfqweek") if item.get(key)), None)
            values = item.get(selected_series) if selected_series is not None else None
        if isinstance(values, list):
            break
    if not isinstance(values, list):
        return pl.DataFrame()

    normalized: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, list) or len(item) < 6:
            continue
        try:
            day = date.fromisoformat(str(item[0])[:10])
        except ValueError:
            continue
        normalized.append({
            "symbol": symbol,
            "asset_type": "stock",
            "source": source,
            "price_adjustment": "unadjusted" if selected_series == "day" else "forward_adjusted",
            # This compatibility parser has no independent raw-price check.
            "raw_price_verified": False,
            "date": day,
            "open": _float(item[1]),
            "high": _float(item[3]),
            "low": _float(item[4]),
            "close": _float(item[2]),
            "volume": _float(item[5]),
            "amount": None,
            "pre_close": None,
            "change_pct": None,
            # Historical bars have a trade date but no source quote timestamp.
            # Keep this null rather than using the HTTP receive time.
            "quote_ts": None,
        })
    if not normalized:
        return pl.DataFrame()
    # The six-value array does not establish currency, units, observation time,
    # or adjustment/verification provenance. Keep those fields explicitly null;
    # a Hong Kong suffix alone cannot distinguish HKD, CNY and USD counters.
    missing_metadata = {
        "currency": pl.String,
        "volume_unit": pl.String,
        "amount_source": pl.String,
        "price_schema_version": pl.Int64,
        "observed_at": pl.String,
        "adjustment_source": pl.String,
        "adjustment_version": pl.String,
        "adjustment_as_of": pl.String,
        "verification_source": pl.String,
    }
    return pl.DataFrame(normalized).with_columns(
        pl.lit(None, dtype=dtype).alias(name) for name, dtype in missing_metadata.items()
    ).select(DAILY_COLS)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _source_timestamp_ms(value: Any, timezone_name: str) -> int | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        formats = ("%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")
        parsed = None
        for fmt in formats:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        return int(parsed.replace(tzinfo=ZoneInfo(timezone_name)).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


class TencentMultiMarketProvider:
    """Candidate HK/US Tencent provider; not enabled by the registry."""

    capabilities = ProviderCapabilities(daily=True, realtime=True)

    def __init__(self, market: Market, *, timeout: float = 5.0) -> None:
        self.market = market
        self.name = f"tencent_{market}_candidate"
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def get_realtime(self, symbols: list[str] | None = None, **_: Any) -> pl.DataFrame:
        if not symbols:
            return pl.DataFrame()
        codes = ",".join(_market_code(s, self.market) for s in symbols)
        try:
            response = self._client.get(
                "https://qt.gtimg.cn/q=" + codes,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
            raw = response.content.decode("gbk", errors="replace")
        except (httpx.HTTPError, UnicodeError):
            return pl.DataFrame()
        rows: list[dict[str, Any]] = []
        for m in _LINE_RE.finditer(raw):
            sym = _key_to_symbol(m.group(1), self.market)
            if sym is None:
                continue
            row = parse_tencent_quote(m.group(0), symbol=sym, market=self.market)
            if row:
                rows.append(row)
        return pl.DataFrame(rows) if rows else pl.DataFrame()

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: AssetType = "stock",
    ) -> pl.DataFrame:
        del asset_type
        if not symbols:
            return pl.DataFrame()
        frames: list[pl.DataFrame] = []
        start = start_time.date().isoformat() if start_time else ""
        end = end_time.date().isoformat() if end_time else ""
        for symbol in symbols:
            code = _market_code(symbol, self.market)
            url = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
            params = {"_var": "kline_dayqfq", "param": f"{code},day,{start},{end},2000,qfqa"}
            try:
                response = self._client.get(url, params=params)
                response.raise_for_status()
                frame = parse_tencent_kline(response.text, symbol=symbol)
                if not frame.is_empty():
                    frames.append(frame)
            except httpx.HTTPError:
                continue
        return pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame()

    def get_instruments(self, asset_type: AssetType) -> pl.DataFrame:
        del asset_type
        return pl.DataFrame()

    def get_adj_factors(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        return pl.DataFrame()

    def get_minute(self, *args: Any, **kwargs: Any) -> pl.DataFrame:
        return pl.DataFrame()
