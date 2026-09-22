"""可恢复的港股/美股日 K 同步任务服务。"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.data_providers.normalizer import normalize_market_symbols
from app.services import kline_sync, preferences
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

SUPPORTED_MARKETS = frozenset({"HK", "US"})
# 自动全量模式的最低规模护栏，防止页面 demo 池被误当作全量快照。
MIN_AUTO_UNIVERSE_SIZE = {"HK": 100, "US": 500}
CheckpointCallback = Callable[[dict[str, Any]], None] | None


class ProviderCircuit:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.failures = 0
        self.opened_at = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at <= 0:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown_seconds:
                self.opened_at = 0.0
                self.failures = 0
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.opened_at = 0.0

    def record_failure(self) -> bool:
        with self._lock:
            self.failures += 1
            if self.failures >= self.failure_threshold:
                self.opened_at = time.monotonic()
            return self.opened_at > 0


class UniverseUnavailableError(ValueError):
    """全量 instruments 快照不可用于自动日 K 同步。"""


def load_market_universe(data_dir: Path, market: str) -> list[str]:
    """从真实市场快照读取冻结 universe，拒绝 demo 降级数据。"""
    market = str(market or "").upper()
    if market not in SUPPORTED_MARKETS:
        raise UniverseUnavailableError(f"不支持的市场: {market}")
    path = data_dir / "instruments" / f"{market.lower()}_instruments.parquet"
    if not path.exists():
        raise UniverseUnavailableError(f"{market} instruments 快照不存在，请先同步标的池")
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:
        raise UniverseUnavailableError(f"{market} instruments 快照读取失败: {exc}") from exc
    required = {"symbol", "source"}
    if not required.issubset(frame.columns):
        raise UniverseUnavailableError(f"{market} instruments 快照缺少字段: {sorted(required - set(frame.columns))}")
    rows = frame.select(["symbol", "source"]).drop_nulls().unique()
    try:
        symbols = normalize_market_symbols(rows.get_column("symbol").cast(pl.Utf8).to_list(), market)
    except ValueError as exc:
        raise UniverseUnavailableError(f"{market} 标的池含跨市场或无效代码") from exc
    sources = set(rows.get_column("source").cast(pl.Utf8).to_list())
    if not symbols:
        raise UniverseUnavailableError(f"{market} instruments 快照为空")
    if sources and sources.issubset({"hk_demo", "us_demo"}):
        raise UniverseUnavailableError(f"{market} instruments 仅包含 demo 标的，不能执行全量同步")
    minimum = MIN_AUTO_UNIVERSE_SIZE[market]
    if len(symbols) < minimum:
        raise UniverseUnavailableError(
            f"{market} instruments 快照仅有 {len(symbols)} 个标的，低于全量同步最低门槛 {minimum}"
        )
    return sorted(symbols)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def universe_fingerprint(symbols: list[str]) -> str:
    payload = "\n".join(sorted(set(symbols))).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def checkpoint_path(data_dir: Path, job_id: str) -> Path:
    return data_dir / "checkpoints" / "market_daily" / f"{job_id}.json"


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    """原子写 checkpoint，避免任务中断留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_checkpoint(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("读取日 K checkpoint 失败: %s: %s", path, exc)
        return None


def _normalize_symbols(symbols: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = str(raw or "").strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _error_code(exc: BaseException) -> str:
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in name or "timeout" in message:
        return "timeout"
    if any(token in message for token in ("rate limit", "too many requests", "429")):
        return "rate_limited"
    if "capability" in name or "capability" in message:
        return "capability_denied"
    return "provider_error"


def _raise_if_cancelled(job_id: str) -> None:
    from app.services.pipeline_jobs import JobCancelledError, is_cancelled
    if is_cancelled(job_id):
        raise JobCancelledError(job_id)



def run_market_daily_sync(
    *,
    repo: KlineRepository,
    capset: CapabilitySet,
    job_id: str,
    market: str,
    symbols: list[str] | None,
    start_date: datetime,
    end_date: datetime,
    mode: str = "full",
    resume_checkpoint: dict[str, Any] | None = None,
    batch_size: int | None = None,
    on_progress: Callable[[str, int, str, int | None, bool], None] | None = None,
    on_checkpoint: CheckpointCallback = None,
    request_timeout_seconds: float | None = 30.0,
    circuit_failure_threshold: int = 3,
    circuit_cooldown_seconds: float = 60.0,
    compute_indicators: bool = False,
) -> dict[str, Any]:
    """执行 HK/US 日 K 同步，按分块完成并持久化恢复状态。"""
    market = str(market or "").upper()
    if market not in SUPPORTED_MARKETS:
        raise ValueError(f"market 只支持 HK/US，收到: {market}")
    if mode not in {"full", "incremental", "retry_only"}:
        raise ValueError(f"mode 不支持: {mode}")

    if symbols is None:
        symbols = load_market_universe(repo.store.data_dir, market)
    universe = normalize_market_symbols(symbols, market)
    requested = list(universe)
    if resume_checkpoint is not None:
        if resume_checkpoint.get("market") != market:
            raise ValueError("checkpoint market 与请求不一致")
        if resume_checkpoint.get("universe_fingerprint") != universe_fingerprint(universe):
            raise ValueError("checkpoint universe 已变化，不能直接恢复")
        failed = _normalize_symbols(resume_checkpoint.get("failed_symbols", []))
        completed = set(_normalize_symbols(resume_checkpoint.get("completed_symbols", [])))
        if mode == "retry_only":
            requested = failed
        else:
            requested = [symbol for symbol in requested if symbol not in completed]
    else:
        completed = set()

    path = checkpoint_path(repo.store.data_dir, job_id)
    failed_symbols: list[str] = []
    skipped_symbols: list[str] = []
    errors: dict[str, str] = {}
    enriched_written = 0
    outcomes: dict[str, dict] = {}
    chunk_size = max(1, int(batch_size or (1 if market == "HK" else 50)))
    chunks = _chunks(requested, chunk_size)
    provider_name = preferences.get_daily_data_provider()
    if provider_name == "tickflow":
        provider_name = kline_sync.daily_sync_capability(capset, market)[0]
    circuit = ProviderCircuit(circuit_failure_threshold, circuit_cooldown_seconds)
    payload: dict[str, Any] = {
        "job_id": job_id,
        "market": market,
        "provider": provider_name,
        "mode": mode,
        "universe_fingerprint": universe_fingerprint(_normalize_symbols(symbols)),
        "coverage_start": start_date.date().isoformat(),
        "coverage_end": end_date.date().isoformat(),
        "symbols_total": len(universe),
        "universe_symbols": universe,
        "completed_symbols": sorted(completed),
        "failed_symbols": [],
        "skipped_symbols": [],
        "attempts": {},
        "provider_errors": {},
        "last_success_symbol": None,
        "last_success_at": None,
        "started_at": _now(),
        "checkpoint_path": str(path),
    }
    write_checkpoint(path, payload)
    if on_checkpoint:
        on_checkpoint(dict(payload))

    for index, chunk in enumerate(chunks, start=1):
        try:
            _raise_if_cancelled(job_id)
        except BaseException as exc:
            from app.services.pipeline_jobs import JobCancelledError
            if not isinstance(exc, JobCancelledError):
                raise
            payload["failed_symbols"] = sorted(set(failed_symbols + [symbol for symbol in requested if symbol not in completed]))
            payload["status"] = "cancelled"
            payload["finished_at"] = _now()
            write_checkpoint(path, payload)
            if on_checkpoint:
                on_checkpoint(dict(payload))
            raise
        if on_progress:
            pct = int((index - 1) * 100 / max(1, len(chunks)))
            on_progress("market_daily_sync", pct, f"{market} 日K同步 {index - 1}/{len(chunks)}", int((index - 1) * 100 / max(1, len(chunks))), True)
        for symbol in chunk:
            payload["attempts"][symbol] = int(payload["attempts"].get(symbol, 0)) + 1
        successful_chunk: list[str] = []
        failed_chunk: list[str] = []
        chunk_items: list[dict] = []
        if not circuit.allow():
            failed_symbols.extend(chunk)
            for symbol in chunk:
                errors[symbol] = "circuit_open"
            payload["failed_symbols"] = sorted(set(failed_symbols))
            payload["provider_errors"] = errors
            payload["circuit_open"] = True
            write_checkpoint(path, payload)
            if on_checkpoint:
                on_checkpoint(dict(payload))
            continue
        try:
            written = kline_sync.sync_and_persist_daily_batch(
                chunk,
                repo,
                capset,
                start_date=start_date,
                end_date=end_date,
                count=None,
                successful_out=successful_chunk,
                failed_out=failed_chunk,
                request_timeout_seconds=request_timeout_seconds,
                asset_type=market.lower(),
                items_out=chunk_items,
                before_publish=lambda: _raise_if_cancelled(job_id),
            )
            outcomes.update({item["symbol"]: item for item in chunk_items})
            enriched_written += sum(bool(item.get("enriched_updated")) for item in chunk_items)
            _raise_if_cancelled(job_id)
            if compute_indicators and successful_chunk:
                from app.services.hk_data_adapter import sync_hk_daily_to_enriched
                from app.tickflow.market_daily import (
                    read_legacy_market_daily,
                    read_market_daily_symbol,
                )

                legacy = read_legacy_market_daily(repo.store.data_dir, market, successful_chunk)
                enriched_success: list[str] = []
                for symbol in successful_chunk:
                    try:
                        item = outcomes.get(symbol, {})
                        if item.get("enriched_available"):
                            enriched_success.append(symbol)
                            continue
                        _raise_if_cancelled(job_id)
                        raw = read_market_daily_symbol(repo.store.data_dir, symbol, legacy=legacy)
                        count_written = sync_hk_daily_to_enriched(
                            symbol, repo.store.data_dir, raw=raw, raise_errors=True,
                            before_publish=lambda: _raise_if_cancelled(job_id),
                        )
                        if count_written:
                            enriched_written += count_written
                            enriched_success.append(symbol)
                            outcomes.setdefault(symbol, {"symbol": symbol}).update(enriched_updated=True)
                        else:
                            errors[symbol] = "enriched_empty"
                    except Exception:
                        logger.warning("%s 指标重算失败 %s", market, symbol, exc_info=True)
                        errors[symbol] = "enriched_failed"
                        outcomes.setdefault(symbol, {"symbol": symbol}).update(status="partial", reason="原始日线已取得, 但指标计算或发布失败", reason_code="enriched_failed", enriched_updated=False)
                successful_chunk = enriched_success
        except BaseException as exc:
            from app.services.pipeline_jobs import JobCancelledError
            if isinstance(exc, JobCancelledError):
                completed.update(_normalize_symbols(successful_chunk))
                failed_symbols.extend(
                    symbol for symbol in chunk
                    if symbol not in successful_chunk and symbol not in failed_symbols
                )
                payload["completed_symbols"] = sorted(completed)
                payload["failed_symbols"] = sorted(set(failed_symbols + [
                    symbol for symbol in requested if symbol not in completed
                ]))
                payload["provider_errors"] = errors
                payload["items"] = [outcomes[symbol] for symbol in requested if symbol in outcomes]
                payload["status"] = "cancelled"
                payload["finished_at"] = _now()
                write_checkpoint(path, payload)
                if on_checkpoint:
                    on_checkpoint(dict(payload))
                raise
            if not isinstance(exc, Exception):
                raise
            code = _error_code(exc)
            circuit_open = circuit.record_failure()
            logger.warning("%s 日K分块失败 (%d/%d): %s (circuit_open=%s)", market, index, len(chunks), exc, circuit_open)
            failed_symbols.extend(chunk)
            for symbol in chunk:
                errors[symbol] = code
            written = 0

        successful = set(_normalize_symbols(successful_chunk))
        if successful:
            circuit.record_success()
        completed.update(successful)
        failed_in_chunk = [symbol for symbol in chunk if symbol not in successful]
        explicit_failed = set(_normalize_symbols(failed_chunk))
        failed_symbols.extend(symbol for symbol in failed_in_chunk if symbol not in failed_symbols)
        for symbol in failed_in_chunk:
            errors.setdefault(
                symbol,
                outcomes.get(symbol, {}).get("reason_code") or ("provider_error" if symbol in explicit_failed else ("empty_result" if written == 0 else "missing_from_result")),
            )
        if successful:
            payload["completed_symbols"] = sorted(completed)
            payload["last_success_symbol"] = sorted(successful)[-1]
            payload["last_success_at"] = _now()
        payload["failed_symbols"] = sorted(set(failed_symbols))
        payload["skipped_symbols"] = sorted(set(skipped_symbols))
        payload["provider_errors"] = errors
        payload["items"] = [outcomes.get(symbol, {"symbol": symbol, "status": "ok" if symbol in completed else "failed", "reason": errors.get(symbol)}) for symbol in requested if symbol in outcomes or symbol in completed or symbol in errors]
        write_checkpoint(path, payload)
        if on_checkpoint:
            on_checkpoint(dict(payload))
        if on_progress:
            pct = int(index * 100 / max(1, len(chunks)))
            on_progress("market_daily_sync", pct, f"{market} 日K同步 {index}/{len(chunks)}", pct, True)

    payload["finished_at"] = _now()
    payload["partial_success"] = bool(payload["failed_symbols"] or payload["skipped_symbols"])
    payload["status"] = (
        "empty" if not universe else
        "failed" if not completed else
        "completed_with_errors" if payload["partial_success"] else "completed"
    )
    payload.update({
        "operation": "daily_download", "requested": len(requested),
        "succeeded": len(set(requested) & completed),
        "failed": len(payload["failed_symbols"]), "skipped": len(payload["skipped_symbols"]),
        "enriched_dates_written": enriched_written,
        "items": [{"symbol": symbol, "status": "ok" if symbol in completed else "failed",
                   "reason": None if symbol in completed else errors.get(symbol, "empty_result"),
                   **outcomes.get(symbol, {})} for symbol in requested],
        "unchanged": sum(outcomes.get(symbol, {}).get("status") == "unchanged" for symbol in requested if symbol in completed),
    })
    from app.services.market_data_status import market_data_generation
    payload["data_generation"] = market_data_generation(repo.store.data_dir, market)
    payload["failures"] = [{"symbol": symbol, "reason": errors.get(symbol, "empty_result")}
                           for symbol in requested if symbol not in completed]
    write_checkpoint(path, payload)
    if on_checkpoint:
        on_checkpoint(dict(payload))
    return payload
