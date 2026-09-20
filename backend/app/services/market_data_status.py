"""Read-only HK/US coverage and explicit indicator-recomputation outcomes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from app.data_providers.normalizer import normalize_lot_size, normalize_market_symbols
from app.markets.hk import HK_TZ
from app.services.kline_sync import daily_sync_capability
from app.tickflow.market_daily import (
    list_market_daily_symbols,
    read_legacy_market_daily,
    read_market_daily_symbol,
)

_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: dict[tuple[str, str], tuple[str, dict]] = {}
_DAILY_FIELDS = ("open", "high", "low", "close", "volume", "amount")
_ENRICHED_FIELDS = (*_DAILY_FIELDS, "change_pct", "ma20", "ma60", "pe_ttm", "pb", "turnover_rate")


def market_data_generation(data_dir: Path, market: str) -> str:
    """Hash the inputs shared by market readers without creating a marker."""
    from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation

    asset = market.lower()
    marker = data_dir / f".matrix_generation_{asset}.json"
    base = get_enriched_generation(data_dir, asset, initialize=False) if marker.exists() else "legacy"
    digest = hashlib.blake2b(base.encode(), digest_size=20)
    files = sorted((data_dir / "kline_hk_us_enriched").glob(f"symbol=*.{market.upper()}/**/*.parquet"))
    instruments = data_dir / "instruments" / f"{asset}_instruments.parquet"
    if instruments.exists():
        files.append(instruments)
    if asset == "hk":
        files.extend(sorted((data_dir / "financials").glob("*/hk.parquet")))
        legacy_financial = data_dir / "financials" / "metrics" / "part.parquet"
        if legacy_financial.exists():
            files.append(legacy_financial)
        files.extend(sorted((data_dir / "adj_factor_hk").glob("symbol=*.HK/part.parquet")))
        files.extend(sorted((data_dir / "hk_data_audit" / "prices").glob("*.HK.json")))
    try:
        for path in files:
            stat = path.stat()
            digest.update(str(path.relative_to(data_dir)).encode())
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    except FileNotFoundError as exc:
        raise EnrichedGenerationUnavailableError("market data files changed") from exc
    current = get_enriched_generation(data_dir, asset, initialize=False) if marker.exists() else "legacy"
    if current != base:
        raise EnrichedGenerationUnavailableError("market data generation changed")
    return f"{asset}:{digest.hexdigest()}"


def hk_financial_capability() -> tuple[bool, str | None]:
    """Inspect configured implementation support without fetching reports."""
    try:
        from app.data_providers.registry import get_default_provider

        provider = get_default_provider("HK", dataset="financial")
        supported = bool(provider.capabilities.financial)
        return supported, None if supported else "港股历史财务来源未配置"
    except (ImportError, ValueError):
        return False, "港股历史财务适配器不可用"


def _hk_price_audit(data_dir: Path, targets: set[str], enriched_files: list[Path]) -> dict:
    verified: dict[str, dict] = {}
    mixed: set[str] = set()
    adjustment_sources: set[str] = set()
    checked: list[str] = []
    warnings: list[str] = []
    all_symbols = targets | {path.parent.name.removeprefix("symbol=") for path in enriched_files}
    for path in enriched_files:
        symbol = path.parent.name.removeprefix("symbol=")
        try:
            names = set(pq.read_schema(path).names)
            required = {"date", "price_schema_version", "raw_price_verified", "price_adjustment", "adjustment_source", "adjustment_version", "adjustment_as_of", "currency", "volume_unit"}
            if not required.issubset(names):
                continue
            frame = pl.read_parquet(path, columns=sorted(required))
            valid = ((pl.col("price_schema_version") == 1) & pl.col("raw_price_verified").fill_null(False)
                     & (pl.col("price_adjustment") == "forward_adjusted")
                     & pl.col("adjustment_source").is_not_null() & pl.col("adjustment_version").is_not_null()
                     & (pl.col("adjustment_as_of").cast(pl.Date) >= pl.col("date").cast(pl.Date))
                     & pl.col("currency").is_in(["HKD", "CNY", "USD"]) & (pl.col("volume_unit") == "share"))
            good = frame.filter(valid.fill_null(False))
            if good.height and good.height < frame.height:
                mixed.add(symbol)
            if good.height:
                verified[symbol] = {"start": _date_text(good["date"].min()), "end": _date_text(good["date"].max()), "rows": good.height}
                adjustment_sources.update(good["adjustment_source"].drop_nulls().unique().to_list())
            audit_path = data_dir / "hk_data_audit" / "prices" / f"{symbol}.json"
            if audit_path.exists():
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                if audit.get("last_checked_at"):
                    checked.append(str(audit["last_checked_at"]))
        except Exception:
            warnings.append(f"{symbol} 的价格口径资料不可读取")
    unknown = all_symbols - set(verified)
    if unknown:
        warnings.append(f"{len(unknown)} 只尚无经过核实的原价和复权窗口, 存量需按维护窗口重新取得")
    return {"status": "verified" if verified and not unknown and not mixed else "partial" if verified else "unknown",
            "verified_symbols": len(verified), "unknown_symbols": len(unknown), "mixed_basis_symbols": len(mixed),
            "adjustment_sources": sorted(adjustment_sources), "last_checked_at": max(checked) if checked else None,
            "warnings": warnings, "verified_ranges": verified}


def _date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value is not None else None


def _frame_stats(frame: pl.DataFrame, fields: tuple[str, ...]) -> dict:
    dates = frame.get_column("date").cast(pl.Date).drop_nulls() if "date" in frame.columns else pl.Series([], dtype=pl.Date)
    return {
        "rows": frame.height,
        "first_date": _date_text(dates.min()), "last_date": _date_text(dates.max()),
        "missing": {name: frame[name].null_count() if name in frame.columns else frame.height for name in fields},
        "unknown": {},
    }


def _footer_stats(path: Path, fields: tuple[str, ...]) -> dict:
    """Read footer statistics only; lack of statistics is reported as unknown."""
    metadata = pq.read_metadata(path)
    schema = metadata.schema.names
    missing: dict[str, int] = {name: 0 for name in fields}
    unknown: dict[str, int] = {name: 0 for name in fields}
    first_dates: list[str] = []
    last_dates: list[str] = []
    for index in range(metadata.num_row_groups):
        group = metadata.row_group(index)
        for name in fields:
            if name not in schema:
                missing[name] += group.num_rows
                continue
            stats = group.column(schema.index(name)).statistics
            if stats is None or not stats.has_null_count:
                unknown[name] += group.num_rows
            else:
                missing[name] += stats.null_count
        if "date" in schema:
            stats = group.column(schema.index("date")).statistics
            if stats is not None and stats.has_min_max:
                first_dates.append(_date_text(stats.min))
                last_dates.append(_date_text(stats.max))
    if metadata.num_rows and not first_dates:
        values = pl.read_parquet(path, columns=["date"]).get_column("date").cast(pl.Date)
        first_dates = [_date_text(values.min())]
        last_dates = [_date_text(values.max())]
    return {"rows": metadata.num_rows, "first_date": min(first_dates) if first_dates else None,
            "last_date": max(last_dates) if last_dates else None, "missing": missing, "unknown": unknown}


def _coverage(
    data_dir: Path, market: str, dataset: str, files: list[Path], targets: set[str],
) -> tuple[dict, dict, list[str]]:
    fields = _DAILY_FIELDS if dataset == "daily" else _ENRICHED_FIELDS
    grouped: dict[str, list[Path]] = {}
    for path in files:
        grouped.setdefault(path.parent.name.removeprefix("symbol="), []).append(path)
    legacy = read_legacy_market_daily(data_dir, market) if dataset == "daily" else pl.DataFrame()
    legacy_symbols = set(legacy.get_column("symbol").to_list()) if not legacy.is_empty() else set()
    stats_by_symbol: dict[str, dict] = {}
    warnings: list[str] = []
    for symbol in sorted(set(grouped) | legacy_symbols):
        try:
            paths = grouped.get(symbol, [])
            if dataset == "daily" and (symbol in legacy_symbols or len(paths) > 1):
                stats = _frame_stats(read_market_daily_symbol(data_dir, symbol, legacy=legacy), fields)
            elif len(paths) == 1:
                stats = _footer_stats(paths[0], fields)
            else:
                frame = pl.concat([pl.read_parquet(path) for path in paths], how="diagonal_relaxed")
                stats = _frame_stats(frame.unique(["symbol", "date"], keep="last"), fields)
            if stats["rows"]:
                stats_by_symbol[symbol] = stats
        except Exception:
            warnings.append(f"{symbol} 的{'日 K' if dataset == 'daily' else '指标'}分区无法读取")
    stored = set(stats_by_symbol)
    first = [stats["first_date"] for stats in stats_by_symbol.values() if stats["first_date"]]
    last = [stats["last_date"] for stats in stats_by_symbol.values() if stats["last_date"]]
    target_last = [stats_by_symbol[symbol]["last_date"] for symbol in stored & targets
                   if stats_by_symbol[symbol]["last_date"]]
    coverage = {
        "symbols": len(stored), "target_symbols": len(stored & targets), "extra_symbols": len(stored - targets),
        "missing_symbols": len(targets - stored),
        "rows": sum(stats["rows"] for stats in stats_by_symbol.values()),
        "target_rows": sum(stats_by_symbol[symbol]["rows"] for symbol in stored & targets),
        "first_date": min(first) if first else None, "last_date": max(last) if last else None,
        "target_last_date": max(target_last) if target_last else None,
    }
    missing = {name: {"missing_rows": sum(stats["missing"].get(name, 0) for stats in stats_by_symbol.values()),
                      "unknown_rows": sum(stats["unknown"].get(name, 0) for stats in stats_by_symbol.values())}
               for name in fields}
    return coverage, missing, warnings


def get_market_data_status(data_dir: Path, market: str, capset: Any = None) -> dict:
    """Report the persisted target pool and extra history without creating markers."""
    market = market.upper()
    normalize_market_symbols([], market)
    instrument_path = data_dir / "instruments" / f"{market.lower()}_instruments.parquet"
    raw_files = sorted((data_dir / "kline_daily").glob(f"symbol=*.{market}/*.parquet"))
    enriched_files = sorted((data_dir / "kline_hk_us_enriched").glob(f"symbol=*.{market}/*.parquet"))
    fingerprint = hashlib.blake2b(digest_size=20)
    all_files = [*raw_files, *enriched_files,
                 *sorted((data_dir / "kline_daily").glob("date=*/*.parquet"))]
    if instrument_path.exists():
        all_files.append(instrument_path)
    marker = data_dir / f".matrix_generation_{market.lower()}.json"
    if marker.exists():
        all_files.append(marker)
    for path in all_files:
        stat = path.stat()
        fingerprint.update(str(path.relative_to(data_dir)).encode())
        fingerprint.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    generation = market_data_generation(data_dir, market)
    provider, supported, reason = daily_sync_capability(capset, market)
    cache_key = (str(data_dir.resolve()), market)
    financial_supported, financial_reason = hk_financial_capability() if market == "HK" else (False, None)
    today = datetime.now(HK_TZ).date()
    cache_version = f"{fingerprint.hexdigest()}:{generation}:{provider}:{supported}:{financial_supported}:{today}"
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(cache_key)
    if cached is not None and cached[0] == cache_version:
        return {**cached[1], "checked_at": datetime.now(UTC).isoformat()}
    instruments = pl.read_parquet(instrument_path) if instrument_path.exists() else pl.DataFrame()
    if "symbol" in instruments.columns:
        instruments = instruments.filter(pl.col("symbol").str.ends_with(f".{market}")).unique("symbol")
    targets = set(instruments.get_column("symbol").to_list()) if "symbol" in instruments.columns else set()
    lots = [normalize_lot_size(value) for value in instruments.get_column("lot_size").to_list()] if "lot_size" in instruments.columns else []
    lot_available = sum(value is not None for value in lots)
    daily, daily_missing, warnings = _coverage(data_dir, market, "daily", raw_files, targets)
    enriched, enriched_missing, enriched_warnings = _coverage(data_dir, market, "enriched", enriched_files, targets)
    warnings.extend(enriched_warnings)
    if not targets:
        warnings.append("没有已保存的市场标的池;默认筛选和回测池为空")
    if daily["missing_symbols"]:
        warnings.append(f"当前标的池有 {daily['missing_symbols']} 只缺少日 K")
    if enriched["missing_symbols"]:
        warnings.append(f"当前标的池有 {enriched['missing_symbols']} 只缺少指标")
    if daily["extra_symbols"] or enriched["extra_symbols"]:
        warnings.append("存量中含当前标的池外的历史证券;默认筛选和回测仅使用当前池")
    if market != "HK":
        warnings.append("旧行情未逐行记录复权和量额来源;存在数据不等于已核实历史复权或财务覆盖")
    sources = sorted(instruments.get_column("source").drop_nulls().unique().to_list()) if "source" in instruments.columns else []
    result = {
        "market": market, "checked_at": datetime.now(UTC).isoformat(),
        "currency": "HKD" if market == "HK" else "USD", "source": sources,
        "data_generation": generation,
        "instruments": {"symbols": len(targets), "lot_size_available": lot_available,
                        "lot_size_missing": len(targets) - lot_available if market == "HK" else 0},
        "daily": daily, "enriched": enriched,
        "missing_fields": {"daily": daily_missing, "enriched": enriched_missing},
        "capabilities": {"daily_download": supported, "daily_provider": provider,
                         "daily_download_reason": reason, "recompute_enriched": daily["symbols"] > 0,
                         "lot_size_sync": market == "HK"},
        "warnings": warnings,
    }
    if market == "HK":
        from app.data_providers.hk_daily_provider import ADJUSTMENT_SOURCES, DAILY_SOURCES
        from app.services.financial_sync import get_hk_financial_status

        currencies = {name: 0 for name in ("HKD", "CNY", "USD", "unknown")}
        instrument_rows = instruments.to_dicts()
        inactive_symbols: set[str] = set()
        for row in instrument_rows:
            currency = str(row.get("currency") or "")
            currencies[currency if currency in currencies else "unknown"] += 1
            if (row.get("instrument_status") not in {"delisted", "temporary_counter_closed", "rights_trading_ended"}
                    or not row.get("instrument_status_source")):
                continue
            try:
                status_date = date.fromisoformat(str(row.get("instrument_status_as_of"))[:10])
            except ValueError:
                continue
            if status_date <= today:
                inactive_symbols.add(row["symbol"])
        statuses = instruments["lot_size_status"].to_list() if "lot_size_status" in instruments.columns else []
        lot_dates = instruments["lot_size_as_of"].cast(pl.Date, strict=False).drop_nulls() if "lot_size_as_of" in instruments.columns else pl.Series([], dtype=pl.Date)
        verified_lots = sum(row["symbol"] not in inactive_symbols and normalize_lot_size(row.get("lot_size")) is not None
                            and row.get("lot_size_status") not in {"missing", "future_snapshot", "conflict"}
                            for row in instrument_rows)
        missing_lots = len(targets) - verified_lots - len(inactive_symbols)
        result["instruments"].update(currencies=currencies, lot_size_future=statuses.count("future_snapshot"),
                                     lot_size_conflicts=statuses.count("conflict"), lot_size_as_of=_date_text(lot_dates.max()),
                                     lot_size_available=verified_lots, lot_size_missing=missing_lots,
                                     verified_not_applicable=len(inactive_symbols))
        if missing_lots:
            result["warnings"].append(f"{missing_lots} 只缺少有效每手数量,回测会跳过这些标的成交")
        decoder_available = importlib.util.find_spec("akshare") is not None and importlib.util.find_spec("py_mini_racer") is not None
        result["daily_sources"] = [{**source, "available": decoder_available if source["role"] == "primary" else True,
                                    "reason": None if decoder_available or source["role"] == "fallback" else "未安装港股可选解码依赖"}
                                   for source in DAILY_SOURCES]
        result["adjustment_sources"] = [{**source, "available": True,
                                         "reason": "复权数据独立获取; 仅可在已验证覆盖区间内使用缓存"} for source in ADJUSTMENT_SOURCES]
        result["verification_sources"] = [{"id": "eastmoney_hk_daily_check", "label": "东方财富原始日线冲突核验",
                                           "available": True, "reason": "主备行情冲突时按交易日期核对完整价格与成交量; 可使用保留原观测时间的历史资料, 缺少对应资料时仍需联网核验"}]
        result["capabilities"].update(financial_history_sync=financial_supported, financial_history_reason=financial_reason)
        result["financials"] = get_hk_financial_status(data_dir)
        result["price_audit"] = _hk_price_audit(data_dir, targets, enriched_files)
        result["warnings"].extend(result["price_audit"]["warnings"])
        if currencies["CNY"] or currencies["USD"] or currencies["unknown"]:
            result["warnings"].append("港股回测资金按 HKD 计价, 非港币或币种未知的柜台不可成交")
    with _STATUS_LOCK:
        _STATUS_CACHE[cache_key] = (cache_version, result)
        while len(_STATUS_CACHE) > 8:
            _STATUS_CACHE.pop(next(iter(_STATUS_CACHE)))
    return result


def recompute_market_enriched(data_dir: Path, market: str, symbols: list[str] | None = None) -> dict:
    """Recalculate existing bars only, returning partial/empty/failure explicitly."""
    from app.services.hk_data_adapter import sync_hk_daily_to_enriched

    market = market.upper()
    selected = list_market_daily_symbols(data_dir, market) if symbols is None else normalize_market_symbols(symbols, market)
    legacy = read_legacy_market_daily(data_dir, market, selected)
    items: list[dict] = []
    written = 0
    for symbol in selected:
        try:
            raw = read_market_daily_symbol(data_dir, symbol, legacy=legacy)
            if raw.is_empty():
                items.append({"symbol": symbol, "status": "skipped", "reason": "没有已下载的日 K"})
                continue
            count = sync_hk_daily_to_enriched(symbol, data_dir, raw=raw, raise_errors=True)
            written += count
            items.append({"symbol": symbol, "status": "ok" if count else "skipped",
                          "reason": None if count else "没有可计算的有效交易日"})
        except Exception:
            items.append({"symbol": symbol, "status": "failed", "reason": "日 K 读取、指标计算或写入失败,已保留原数据"})
    succeeded = sum(item["status"] == "ok" for item in items)
    failed = sum(item["status"] == "failed" for item in items)
    skipped = len(items) - succeeded - failed
    status = "completed" if succeeded and not failed and not skipped else "completed_with_errors" if succeeded else "failed" if failed else "empty"
    return {"status": status, "operation": "enriched_recompute", "market": market,
            "requested": len(selected), "succeeded": succeeded, "failed": failed, "skipped": skipped,
            "enriched_dates_written": written, "symbols": selected, "items": items,
            "failures": [{"symbol": item["symbol"], "reason": item["reason"]} for item in items if item["status"] != "ok"],
            "data_generation": market_data_generation(data_dir, market)}
