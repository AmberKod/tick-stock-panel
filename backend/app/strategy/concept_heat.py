"""Current concept membership and cross-sectional heat, isolated by market.

The source is a membership snapshot, not point-in-time historical data. Its
timestamps identify and invalidate a snapshot; they never authorize backfills.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.services.ext_data import ExtConfigStore

SOURCE_ID = "ext_gn_ths"
MIN_MEMBERS = 3
HISTORICAL_REASON = "概念热度不可计算:缺少目标日期可追溯的概念成分,当前概念快照不支持历史筛选或回测"
_PAIR_SCHEMA = {"market": pl.String, "symbol": pl.String, "concept": pl.String}
_SYMBOL_PATTERNS = {
    "cn": r"^\d{6}\.(SH|SZ|BJ)$",
    "hk": r"^\d{1,5}\.HK$",
    # Same class-share/digit grammar as normalize_market_symbols; a complete
    # market suffix is additionally mandatory for membership joins.
    "us": r"^[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*\.US$",
}
_cache_lock = threading.Lock()
_mapping_cache: dict[tuple[str, str], tuple[tuple, ConceptMappingSnapshot]] = {}


@dataclass(frozen=True)
class ConceptMappingSnapshot:
    """One read-only membership version and its capability status."""

    pairs: pl.DataFrame
    status: str
    reason: str
    version: str
    updated_at: str | None


def concept_market(asset_type: str) -> str:
    """Return the market namespace without treating ETF membership as stocks."""
    value = str(asset_type).strip().lower()
    return "cn" if value == "stock" else value


def _signature(directory: Path) -> tuple:
    signatures: list[tuple[int, int] | None] = []
    for name in ("config.json", "part.parquet"):
        try:
            stat = (directory / name).stat()
            signatures.append((stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            signatures.append(None)
    return tuple(signatures)


def _empty_snapshot(status: str, reason: str, version: str, updated_at: str | None = None) -> ConceptMappingSnapshot:
    return ConceptMappingSnapshot(pl.DataFrame(schema=_PAIR_SCHEMA), status, reason, version, updated_at)


def _symbol_expr(market: str) -> pl.Expr:
    symbol = pl.col("symbol").cast(pl.String, strict=False).str.strip_chars().str.to_uppercase()
    valid = symbol.str.contains(_SYMBOL_PATTERNS.get(market, r"a^"))
    normalized = symbol.str.replace(r"\.HK$", "").str.zfill(5) + ".HK" if market == "hk" else symbol
    return pl.when(valid).then(normalized).otherwise(None)


def load_concept_mapping(data_dir: Path | None, market: str = "cn") -> ConceptMappingSnapshot:
    """Read exactly one ext source, caching by directory, market and file versions.

    Reads occur outside the lock. A changed source is retried once before it is
    reported unavailable, so metadata never labels pairs from another version.
    """
    market = concept_market(market)
    if data_dir is None:
        return _empty_snapshot("missing_source", "未配置概念数据目录", "missing")
    directory_key = os.path.normcase(str(Path(data_dir).resolve()))
    directory = Path(directory_key) / "ext_data" / SOURCE_ID
    cache_key = (directory_key, market)
    for _ in range(2):
        try:
            signature = _signature(directory)
        except OSError:
            return _empty_snapshot("read_error", "概念数据文件不可读取", "unreadable")
        version = hashlib.sha256(repr((cache_key, signature)).encode()).hexdigest()[:20]
        with _cache_lock:
            cached = _mapping_cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                return cached[1]

        updated_at: str | None = None
        if market not in _SYMBOL_PATTERNS:
            snapshot = _empty_snapshot("unsupported_asset", "当前资产类型没有已验证的概念映射", version)
        elif signature[0] is None or signature[1] is None:
            snapshot = _empty_snapshot("missing_source", "缺少概念映射文件或配置", version)
        else:
            try:
                config = ExtConfigStore(Path(directory_key)).get(SOURCE_ID)
                if config is None or config.id != SOURCE_ID or config.mode != "snapshot":
                    snapshot = _empty_snapshot("invalid_source", "概念源配置无效或不是受支持的快照", version)
                else:
                    # ExtConfig supplies a default current timestamp when absent;
                    # only the value actually persisted by the source is evidence.
                    raw_config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
                    declared = raw_config.get("updated_at")
                    updated_at = declared if isinstance(declared, str) and declared.strip() else None
                    frame = pl.read_parquet(directory / "part.parquet")
                    if not {"symbol", "所属概念"}.issubset(frame.columns):
                        snapshot = _empty_snapshot("invalid_source", "概念数据缺少 symbol 或所属概念字段", version, updated_at)
                    else:
                        pairs = (
                            frame.select(
                                _symbol_expr(market).alias("symbol"),
                                pl.col("所属概念").cast(pl.String, strict=False).str.split(";").alias("concept"),
                            )
                            .filter(pl.col("symbol").str.contains(_SYMBOL_PATTERNS[market]))
                            .explode("concept")
                            .with_columns(pl.col("concept").str.strip_chars())
                            .filter(pl.col("concept").is_not_null() & (pl.col("concept") != ""))
                            .with_columns(pl.lit(market).alias("market"))
                            .select(list(_PAIR_SCHEMA))
                            .unique(maintain_order=True)
                        )
                        snapshot = ConceptMappingSnapshot(pairs, "available", "", version, updated_at) if not pairs.is_empty() else _empty_snapshot(
                            "missing_mapping", "当前市场没有有效概念映射", version, updated_at,
                        )
            except (OSError, ValueError, TypeError, pl.exceptions.PolarsError):
                snapshot = _empty_snapshot("read_error", "概念数据读取失败,请检查源文件", version, updated_at)
        try:
            if _signature(directory) != signature:
                continue
        except OSError:
            continue
        with _cache_lock:
            _mapping_cache[cache_key] = (signature, snapshot)
        return snapshot
    return _empty_snapshot("source_changed", "概念源正在更新,请稍后重试", "changing")


def concept_heat_required(scoring: Mapping[str, Any] | None, feature_names: Collection[str] = ()) -> bool:
    """A zero weight alone does not opt into concept data or its time checks."""
    return bool((scoring or {}).get("concept_heat")) or "concept_heat" in feature_names


def attach_concept_heat(
    frame: pl.DataFrame, snapshot: ConceptMappingSnapshot, *, market: str = "cn",
    diagnostics: dict[str, int] | None = None,
) -> pl.DataFrame:
    """Attach Float64 heat from complete date/time slices, preserving input rows.

    Each symbol contributes its last finite value once per slice. A concept
    needs three valid distinct members. A stock receives the mean of its valid
    concept heats, including when its own return is unavailable.
    """
    frame = frame.drop("concept_heat", strict=False)
    null_heat = pl.lit(None, dtype=pl.Float64).alias("concept_heat")
    if frame.is_empty():
        return frame.with_columns(pl.Series("concept_heat", [], dtype=pl.Float64))
    if not {"symbol", "change_pct"}.issubset(frame.columns) or snapshot.status != "available":
        return frame.with_columns(null_heat)
    market = concept_market(market)
    pairs = snapshot.pairs.filter(pl.col("market") == market).select("symbol", "concept")
    if pairs.is_empty():
        return frame.with_columns(null_heat)
    time_keys = [name for name in ("date", "datetime") if name in frame.columns]
    keys = [*time_keys, "_concept_symbol"]
    members = frame.select(
        *time_keys,
        _symbol_expr(market).alias("_concept_symbol"),
        pl.col("change_pct").cast(pl.Float64, strict=False).alias("_concept_change"),
    )
    pairs = pairs.rename({"symbol": "_concept_symbol"})
    if diagnostics is not None:
        input_count = frame.select(pl.coalesce(_symbol_expr(market), pl.col("symbol").cast(pl.String))).to_series().n_unique()
        mapped_count = members.select("_concept_symbol").unique().join(pairs.select("_concept_symbol").unique(), on="_concept_symbol").height
        diagnostics.update(input_symbols=input_count, mapped_symbols=mapped_count,
                           unmapped_symbols=input_count - mapped_count,
                           computable_symbols=0, missing_valid_concept_symbols=mapped_count, valid_concepts=0)
    values = (
        members.filter(pl.col("_concept_change").is_finite())
        .unique(subset=keys, keep="last", maintain_order=True)
        .join(pairs, on="_concept_symbol", how="inner")
        .group_by([*time_keys, "concept"])
        .agg(pl.col("_concept_change").mean().alias("_concept_mean"), pl.len().alias("_concept_members"))
        .filter(pl.col("_concept_members") >= MIN_MEMBERS)
    )
    if values.is_empty():
        return frame.with_columns(null_heat)
    stock_heat = (
        members.select(keys).unique()
        .join(pairs, on="_concept_symbol", how="inner")
        .join(values, on=[*time_keys, "concept"], how="inner")
        .group_by(keys)
        .agg(pl.col("_concept_mean").mean().alias("concept_heat"))
    )
    if diagnostics is not None:
        valid_count = stock_heat["_concept_symbol"].n_unique()
        diagnostics.update(computable_symbols=valid_count, valid_concepts=values["concept"].n_unique(),
                           missing_valid_concept_symbols=diagnostics["mapped_symbols"] - valid_count)
    return (
        frame.with_columns(_symbol_expr(market).alias("_concept_symbol"))
        .join(stock_heat, on=keys, how="left", maintain_order="left")
        .drop("_concept_symbol")
    )


def concept_heat_availability(
    snapshot: ConceptMappingSnapshot,
    *,
    market: str,
    as_of: date,
    quote_date: date | None,
    current_market_date: date,
    historical: bool = False,
) -> dict[str, Any]:
    """Describe availability without interpreting source mtime as history."""
    metadata: dict[str, Any] = {
        "source_id": SOURCE_ID, "market": concept_market(market),
        "mapping_version": snapshot.version, "mapping_updated_at": snapshot.updated_at,
        "quote_date": str(quote_date) if quote_date else None,
        "current_market_date": str(current_market_date),
        "aggregation": "mean", "min_members": MIN_MEMBERS,
        "status": "available", "reason_code": None, "reason": "",
    }
    failure: tuple[str, str] | None = None
    if historical:
        failure = ("historical_membership", HISTORICAL_REASON)
    elif snapshot.status != "available":
        failure = (snapshot.status, snapshot.reason)
    elif as_of != current_market_date:
        failure = ("noncurrent_target", f"概念快照仅支持市场当日 {current_market_date},请求日期为 {as_of}")
    elif quote_date is None:
        failure = ("missing_quote_date", "缺少可验证的行情日期")
    elif quote_date < current_market_date:
        failure = ("stale_quote", f"行情日期 {quote_date} 早于当前市场日期 {current_market_date},概念热度不可计算")
    elif quote_date > current_market_date:
        failure = ("future_quote", f"行情日期 {quote_date} 晚于当前市场日期 {current_market_date},概念热度不可计算")
    if failure:
        metadata.update(status="unavailable", reason_code=failure[0], reason=failure[1])
    return metadata


def concept_quote_date(frame: pl.DataFrame | None) -> tuple[date | None, bool]:
    """Read actual date/time columns, rejecting missing and mixed trading dates."""
    if frame is None or frame.is_empty():
        return None, False
    columns = [name for name in ("date", "datetime") if name in frame.columns]
    if not columns:
        return None, False
    dates: set[date] = set()
    missing = False
    try:
        for name in columns:
            values = frame[name].cast(pl.Date, strict=False)
            missing = missing or values.null_count() > 0
            dates.update(values.drop_nulls().to_list())
    except pl.exceptions.PolarsError:
        return None, False
    return (next(iter(dates)) if len(dates) == 1 and not missing else None), len(dates) > 1


def concept_scoring_column(
    repo: Any, *, asset_type: str = "stock", context: str = "current", as_of: date | None = None,
) -> dict[str, Any]:
    """Describe current scoring capability through the existing quote service."""
    from app.markets import get_profile
    from app.services.screener import ScreenerService

    market = concept_market(asset_type)
    today = get_profile(market if market != "etf" else "cn").today()
    snapshot = load_concept_mapping(repo.store.data_dir, market)
    quote_date: date | None = None
    quote_error = False
    try:
        quote_date = ScreenerService(repo, asset_type=asset_type).latest_date()
    except Exception:
        quote_error = True
    metadata = concept_heat_availability(snapshot, market=market, as_of=as_of or today,
                                         quote_date=quote_date, current_market_date=today,
                                         historical=context == "historical")
    if metadata["status"] == "available":
        try:
            quotes, actual_date = repo.get_enriched_latest_asset(asset_type, refresh=False)
            frame_date, mixed_dates = concept_quote_date(quotes)
            metadata = concept_heat_availability(snapshot, market=market, as_of=as_of or today,
                                                 quote_date=frame_date, current_market_date=today)
            if mixed_dates:
                metadata.update(status="unavailable", reason_code="mixed_quote_dates", reason="行情包含多个交易日期")
            elif actual_date != today or quotes is None or quotes.is_empty():
                metadata.update(status="unavailable", reason_code="missing_quote", reason="缺少市场当日行情截面")
            elif metadata["status"] == "available" and (
                "change_pct" not in quotes.columns or not quotes.select(
                    pl.col("change_pct").cast(pl.Float64, strict=False).is_finite().any()
                ).item()
            ):
                metadata.update(status="unavailable", reason_code="missing_change_pct", reason="当日涨幅尚未就绪,请先刷新行情指标")
            elif metadata["status"] == "available":
                counts: dict[str, int] = {}
                attach_concept_heat(quotes, snapshot, market=market, diagnostics=counts)
                metadata.update(counts)
                if not counts.get("computable_symbols"):
                    metadata.update(status="unavailable", reason_code="insufficient_members", reason="没有至少 3 个独立有效成员的概念")
                elif counts["computable_symbols"] < counts["input_symbols"]:
                    metadata["status"] = "partial"
        except Exception:
            metadata.update(status="unavailable", reason_code="quote_read_error", reason="当前行情读取失败,请稍后重试")
    elif quote_error and metadata["reason_code"] == "missing_quote_date":
        metadata.update(reason_code="quote_read_error", reason="当前行情读取失败,请稍后重试")
    return {
        "id": "concept_heat", "label": "概念热度", "group": "题材",
        "desc": "同市场当日概念成员平均涨幅,再对个股所属有效概念取均值;每个概念至少 3 个有效成员。当前快照不支持历史回测。",
        "available": metadata["status"] in {"available", "partial"}, "reason": metadata["reason"], "metadata": metadata,
    }


def concept_cache_identity(metadata: Mapping[str, Any] | None) -> tuple:
    """Identity shared by result caches and the same-day union of past hits."""
    return tuple((metadata or {}).get(name) for name in (
        "market", "quote_date", "current_market_date", "mapping_version", "config_fingerprint",
    ))


def concept_cache_unavailable(
    data_dir: Path, result: Mapping[str, Any], *, market: str, config_fingerprint: str | None,
) -> dict[str, Any] | None:
    """Return explanatory metadata when a cached result is no longer valid."""
    from app.markets import get_profile

    previous = result.get("concept_heat_metadata") or {}
    if not previous and config_fingerprint is None:
        return None
    market = concept_market(market)
    today = get_profile(market if market != "etf" else "cn").today()
    snapshot = load_concept_mapping(data_dir, market)
    try:
        quote_date = date.fromisoformat(str(previous.get("quote_date")))
    except ValueError:
        quote_date = None
    try:
        as_of = date.fromisoformat(str(result.get("as_of")))
    except ValueError:
        as_of = date.min
    metadata = concept_heat_availability(snapshot, market=market, as_of=as_of,
                                         quote_date=quote_date, current_market_date=today)
    if metadata["status"] == "unavailable":
        if metadata["reason_code"] == "missing_quote_date" and previous.get("status") == "unavailable" and previous.get("reason"):
            metadata.update(reason_code=previous.get("reason_code") or "calculation_unavailable", reason=previous["reason"])
        return metadata
    reason = ""
    if previous.get("market") != market:
        reason = "概念热度缓存所属市场已变化,请重新运行策略"
    elif previous.get("mapping_version") != snapshot.version:
        reason = "概念映射已更新,请重新运行策略"
    elif previous.get("current_market_date") != str(today):
        reason = "概念热度缓存已跨市场日期,请重新运行策略"
    elif previous.get("config_fingerprint") != config_fingerprint:
        reason = "策略评分配置已更新,请重新运行策略"
    elif previous.get("status") not in {"available", "partial"}:
        reason = str(previous.get("reason") or "概念热度尚不可计算,请重新运行策略")
    if reason:
        metadata.update(status="unavailable", reason_code="stale_cache", reason=reason)
        return metadata
    return None
