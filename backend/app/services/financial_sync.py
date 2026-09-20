"""财务数据独立同步服务。

解耦于 K-line 管道, 自有调度 + 自有存储。
能力门控: Cap.FINANCIAL (Expert 套餐)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.tickflow.capabilities import Cap, CapabilitySet

logger = logging.getLogger(__name__)

# 每个 API 请求最多 100 个标的
_BATCH_SIZE = 100

# 财务报表 + 历史股本表
FINANCIAL_TABLES = ("metrics", "income", "balance_sheet", "cash_flow", "shares")


# ================================================================
# 同步函数
# ================================================================

def _get_symbols(data_dir: Path) -> list[str]:
    """从 instruments 表获取标的列表。"""
    inst_path = data_dir / "instruments" / "instruments.parquet"
    if not inst_path.exists():
        return []
    try:
        df = pl.read_parquet(inst_path, columns=["symbol"])
        return df["symbol"].to_list()
    except Exception as e:
        logger.warning("读取 instruments 失败: %s", e)
        return []


def _financial_is_custom() -> bool:
    """当前财务数据源是否走 custom (用于绕过 TickFlow Expert 套餐门槛)。"""
    from app.services import preferences
    provider = preferences.get_financial_provider()
    if provider == "tickflow":
        return False
    from app.data_providers import custom as custom_sources
    return custom_sources.provider_has_dataset(provider, "financial")


def _fetch_table(
    table: str,
    symbols: list[str],
    capset: CapabilitySet,
    latest_only: bool = True,
) -> pl.DataFrame:
    """通过当前财务数据源拉取一张标准化财务表。"""
    is_custom = _financial_is_custom()
    if not is_custom and not capset.has(Cap.FINANCIAL):
        logger.info("sync_%s skipped: no FINANCIAL capability", table)
        return pl.DataFrame()
    if not symbols:
        logger.warning("sync_%s skipped: no symbols", table)
        return pl.DataFrame()

    # 自定义数据源分流
    if is_custom:
        from app.data_providers import custom as custom_sources
        from app.services import preferences
        try:
            provider = custom_sources.get_provider(preferences.get_financial_provider())
            df = provider.get_financials(table, symbols, latest_only=latest_only)
        except Exception as e:
            logger.warning("sync_%s custom provider failed: %s", table, e)
            return pl.DataFrame()
        if df.is_empty() or "symbol" not in df.columns:
            return pl.DataFrame()
        return df

    from app.tickflow.client import get_client
    tf = get_client()

    # 分批拉取
    api_method = {
        "metrics": tf.financials.metrics,
        "income": tf.financials.income,
        "balance_sheet": tf.financials.balance_sheet,
        "cash_flow": tf.financials.cash_flow,
        "shares": getattr(tf.financials, "shares", None),
    }[table]
    if api_method is None:
        logger.warning("sync_shares skipped: current TickFlow SDK does not support shares")
        return pl.DataFrame()

    all_records: list[dict] = []
    total_batches = (len(symbols) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for i in range(0, len(symbols), _BATCH_SIZE):
        chunk = symbols[i : i + _BATCH_SIZE]
        batch_num = i // _BATCH_SIZE + 1
        try:
            data = api_method(chunk, latest=latest_only)
            # data 格式: { "600519.SH": [record, ...], ... }
            if isinstance(data, dict):
                for sym, records in data.items():
                    if isinstance(records, list):
                        for rec in records:
                            if isinstance(rec, dict):
                                rec["symbol"] = sym
                                all_records.append(rec)
            logger.debug("sync_%s batch %d/%d: %d records", table, batch_num, total_batches, len(data) if isinstance(data, dict) else 0)
        except Exception as e:
            logger.warning("sync_%s batch %d/%d failed: %s", table, batch_num, total_batches, e)

    if not all_records:
        return pl.DataFrame()

    df = pl.DataFrame(all_records)
    if df.is_empty() or "symbol" not in df.columns:
        return pl.DataFrame()
    return df


def _write_table(table: str, df: pl.DataFrame, data_dir: Path) -> int:
    if df.is_empty() or "symbol" not in df.columns:
        return 0

    # 写入 Parquet (全量覆盖)
    out_dir = data_dir / "financials" / table
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "part.parquet"
    df.write_parquet(out_file)

    logger.info("sync_%s done: %d records written", table, len(df))
    return len(df)


def _sync_table(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
    latest_only: bool = True,
) -> int:
    """同步单张财务表。返回写入的行数。"""
    return _write_table(
        table,
        _fetch_table(table, symbols, capset, latest_only=latest_only),
        data_dir,
    )


def _merge_report_history(*frames: pl.DataFrame) -> pl.DataFrame:
    valid = [
        frame
        for frame in frames
        if not frame.is_empty() and {"symbol", "period_end"} <= set(frame.columns)
    ]
    if not valid:
        return pl.DataFrame()
    merged = (
        pl.concat(valid, how="diagonal_relaxed")
        .filter(pl.col("symbol").is_not_null() & pl.col("period_end").is_not_null())
    )
    # 同一 (symbol, period_end) 多条时保留 announce_date 最新一条 (业绩修正以最新公告为准)。
    if "announce_date" in merged.columns:
        merged = merged.sort(["symbol", "period_end", "announce_date"], nulls_last=True)
    return merged.unique(subset=["symbol", "period_end"], keep="last").sort(
        ["symbol", "period_end"]
    )


def _sync_history_table_for_symbols(
    table: str,
    symbols: list[str],
    data_dir: Path,
    capset: CapabilitySet,
) -> int:
    """历史累积同步: 保留已有各期记录, 仅拉最新期 + 为新标的补全量历史。

    与 shares 同一模式。若改为 latest_only 全量覆盖, 历史各期会在每次同步时
    被冲掉, 财务因子将永远只有单期快照, 任何回测都是未来函数。
    """
    existing = get_financial_df(data_dir, table)
    if existing.is_empty() or not {"symbol", "period_end"} <= set(existing.columns):
        return _sync_table(table, symbols, data_dir, capset, latest_only=False)

    existing_symbols = set(existing["symbol"].drop_nulls().to_list())
    missing_symbols = [symbol for symbol in symbols if symbol not in existing_symbols]
    missing_history = (
        _fetch_table(table, missing_symbols, capset, latest_only=False)
        if missing_symbols
        else pl.DataFrame()
    )
    current_symbols = [symbol for symbol in symbols if symbol in existing_symbols]
    latest = _fetch_table(table, current_symbols, capset, latest_only=True)
    merged = _merge_report_history(existing, missing_history, latest)
    return _write_table(table, merged, data_dir)


def sync_metrics(data_dir: Path, capset: CapabilitySet) -> int:
    """同步核心财务指标 (metrics), 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("metrics", symbols, data_dir, capset)


def sync_income(data_dir: Path, capset: CapabilitySet) -> int:
    """同步利润表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("income", symbols, data_dir, capset)


def sync_balance_sheet(data_dir: Path, capset: CapabilitySet) -> int:
    """同步资产负债表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("balance_sheet", symbols, data_dir, capset)


def sync_cash_flow(data_dir: Path, capset: CapabilitySet) -> int:
    """同步现金流量表, 历史各期累积保留。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("cash_flow", symbols, data_dir, capset)


def sync_shares(data_dir: Path, capset: CapabilitySet) -> int:
    """同步历史股本表。"""
    symbols = _get_symbols(data_dir)
    return _sync_history_table_for_symbols("shares", symbols, data_dir, capset)


def sync_all(data_dir: Path, capset: CapabilitySet) -> dict[str, int]:
    """同步所有财务表。返回 {table: rows}。"""
    if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
        logger.info("sync_all financials skipped: no FINANCIAL capability")
        return {}

    symbols = _get_symbols(data_dir)
    results: dict[str, int] = {}
    for table in FINANCIAL_TABLES:
        results[table] = _sync_history_table_for_symbols(
            table, symbols, data_dir, capset
        )

    # 同步完成后注册 DuckDB 视图
    _refresh_financials_views(data_dir)

    return results


# ================================================================
# DuckDB 视图
# ================================================================

def _refresh_financials_views(data_dir: Path) -> None:
    """刷新财务表 DuckDB 视图 (在 DataStore.db 上注册)。"""
    d = data_dir.as_posix()
    views = {
        "financials_metrics": f"{d}/financials/metrics/*.parquet",
        "financials_income": f"{d}/financials/income/*.parquet",
        "financials_balance_sheet": f"{d}/financials/balance_sheet/*.parquet",
        "financials_cash_flow": f"{d}/financials/cash_flow/*.parquet",
        "financials_shares": f"{d}/financials/shares/*.parquet",
    }
    for name, _path in views.items():
        out = data_dir / "financials" / name.replace("financials_", "") / "part.parquet"
        if not out.exists():
            continue
        # 视图注册需要由 DataStore 完成,这里只做日志
        logger.debug("financial parquet ready: %s (%d rows)", name, out.stat().st_size)


def get_financial_df(data_dir: Path, table: str, *, market: str | None = None) -> pl.DataFrame:
    """读取本地财务 Parquet。"""
    if table not in FINANCIAL_TABLES:
        raise ValueError("unsupported financial table")
    if market is not None and market.upper() == "HK":
        from app.data_providers.hk_financial_provider import normalize_hk_financial_frame

        frames = []
        for filename in ("hk.parquet", "part.parquet"):
            candidate = data_dir / "financials" / table / filename
            if not candidate.exists():
                continue
            try:
                frame = normalize_hk_financial_frame(pl.read_parquet(candidate))
            except Exception as exc:
                logger.warning("读取港股历史财务 %s/%s 失败: %s", table, filename, type(exc).__name__)
                continue
            if not frame.is_empty():
                frames.append(frame)
        merged, _ = _merge_hk_report_history(*frames)
        return merged
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as e:
        logger.warning("读取 financials/%s 失败: %s", table, e)
        return pl.DataFrame()


_HK_FINANCIAL_WRITE_LOCK = threading.Lock()
_HK_VERSION_KEY = ("symbol", "period_end", "announce_date", "revision_id", "source")


def _hk_version_content(row: dict[str, Any]) -> str:
    return json.dumps({key: value for key, value in row.items() if key != "observed_at"},
                      default=str, sort_keys=True, ensure_ascii=False)


def _merge_hk_report_history(*frames: pl.DataFrame) -> tuple[pl.DataFrame, list[dict]]:
    """Retain every disclosure version; keep the first content on identity conflict."""
    from app.data_providers.hk_financial_provider import normalize_hk_financial_frame

    versions: dict[tuple, dict] = {}
    conflicts: list[dict] = []
    for frame in frames:
        normalized = normalize_hk_financial_frame(frame)
        for row in normalized.iter_rows(named=True):
            key = tuple(row.get(name) for name in _HK_VERSION_KEY)
            previous = versions.get(key)
            if previous is not None:
                if _hk_version_content(previous) != _hk_version_content(row):
                    conflicts.append({"symbol": row["symbol"], "period_end": str(row["period_end"]),
                                      "announce_date": str(row["announce_date"]), "revision_id": row["revision_id"],
                                      "source": row["source"], "reason_code": "financial_version_conflict"})
                continue
            versions[key] = row
    if not versions:
        return pl.DataFrame(), conflicts
    return pl.DataFrame(list(versions.values()), infer_schema_length=None).sort(list(_HK_VERSION_KEY)), conflicts


def _get_hk_primary_provider():
    """Use an explicitly configured financial provider, never the A-share SDK."""
    from app.data_providers import custom as custom_sources
    from app.services import preferences

    name = preferences.get_financial_provider()
    if name != "tickflow" and custom_sources.provider_has_dataset(name, "financial"):
        return custom_sources.get_provider(name)
    return None


def _get_hk_fallback_provider():
    from app.data_providers.registry import get_default_provider

    return get_default_provider("HK", dataset="financial")


def _hk_symbols(data_dir: Path) -> list[str]:
    path = data_dir / "instruments" / "hk_instruments.parquet"
    if not path.exists():
        return []
    try:
        frame = pl.read_parquet(path, columns=["symbol"])
        return sorted({str(value) for value in frame["symbol"].drop_nulls() if re.fullmatch(r"[0-9]{5}\.HK", str(value))})
    except Exception as exc:
        logger.warning("读取港股财务股票池失败: %s", type(exc).__name__)
        return []


def get_hk_financial_status(data_dir: Path) -> dict:
    """Describe actual stored field coverage without starting a network request."""
    from app.data_providers.hk_financial_provider import HK_FINANCIAL_FIELDS, HK_RATIO_FIELDS

    frame = get_financial_df(data_dir, "metrics", market="HK")
    pool = set(_hk_symbols(data_dir))
    covered = set(frame["symbol"].to_list()) if not frame.is_empty() else set()
    denominator = pool or covered
    fields = {}
    for field in HK_FINANCIAL_FIELDS:
        valid = frame.filter(pl.col(field).is_finite()) if field in frame.columns else pl.DataFrame()
        symbols = set(valid["symbol"].to_list()) if not valid.is_empty() else set()
        fields[field] = {"available_symbols": len(symbols & denominator), "missing_symbols": len(denominator - symbols),
                         "first_announce_date": str(valid["announce_date"].min()) if symbols else None,
                         "last_announce_date": str(valid["announce_date"].max()) if symbols else None}
    usable = any(fields[field]["available_symbols"] for field in HK_RATIO_FIELDS)
    complete = bool(denominator) and all(fields[field]["missing_symbols"] == 0 for field in HK_FINANCIAL_FIELDS)
    return {
        "status": "available" if complete else "partial" if usable else "unavailable",
        "reason": None if complete else "已核对公告的字段可用;缺少历史每股基准或原始财务依据的字段保持缺失" if usable else "尚无具有公告日期、版本及字段依据的港股历史财务数据",
        "sources": sorted(frame["source"].unique().to_list()) if not frame.is_empty() else [],
        "rows": frame.height, "symbols": len(covered),
        "first_period_end": str(frame["period_end"].min()) if not frame.is_empty() else None,
        "last_period_end": str(frame["period_end"].max()) if not frame.is_empty() else None,
        "first_announce_date": str(frame["announce_date"].min()) if not frame.is_empty() else None,
        "last_announce_date": str(frame["announce_date"].max()) if not frame.is_empty() else None,
        "fields": fields,
    }


def sync_hk_financial_history(
    data_dir: Path, capset=None, *, symbols: list[str] | None = None,
    on_progress=None, job_id: str | None = None,
) -> dict:
    """Fetch missing HK history and publish it separately from all CN tables."""
    from app.data_providers.hk_financial_provider import (
        HK_FINANCIAL_FIELDS,
        HK_RATIO_FIELDS,
        normalize_hk_financial_frame,
    )
    from app.services.hk_data_adapter import _marker_bytes, _publish_hk_files

    selected = list(dict.fromkeys(_hk_symbols(data_dir) if symbols is None else symbols))
    if any(not isinstance(symbol, str) or re.fullmatch(r"[0-9]{5}\.HK", symbol) is None for symbol in selected):
        raise ValueError("港股历史财务只接受五位代码.HK")
    try:
        primary = _get_hk_primary_provider()
    except Exception as exc:
        logger.warning("港股主财务源不可用: %s", type(exc).__name__)
        primary = None
    fallback = _get_hk_fallback_provider()
    staged: list[pl.DataFrame] = []
    items: list[dict] = []
    original = get_financial_df(data_dir, "metrics", market="HK")
    for index, symbol in enumerate(selected):
        if _hk_financial_job_cancelled(job_id):
            items.extend({"symbol": value, "status": "skipped", "reason_code": "cancelled", "reason": "任务已取消"} for value in selected[index:])
            break
        frames = []
        attempted = []
        errors = []
        fallback_rows = 0
        for provider in (primary, fallback):
            if provider is None:
                continue
            attempted.append(str(getattr(provider, "name", "financial")))
            try:
                frame = normalize_hk_financial_frame(provider.get_financials("metrics", [symbol], latest_only=False))
                if not frame.is_empty():
                    frame = frame.filter(pl.col("symbol") == symbol)
                    if not frame.is_empty():
                        frames.append(frame)
                        if provider is fallback:
                            fallback_rows += frame.height
            except Exception as exc:
                errors.append(type(exc).__name__)
        local = original.filter(pl.col("symbol") == symbol) if not original.is_empty() else pl.DataFrame()
        merged, conflicts = _merge_hk_report_history(local, *frames)
        available = sorted(field for field in HK_FINANCIAL_FIELDS if field in merged.columns and merged[field].is_finite().any())
        incoming = bool(frames)
        status = "ok" if incoming and not conflicts and all(field in available for field in HK_RATIO_FIELDS) else "partial" if not merged.is_empty() else "failed"
        reason = "已同步经公告核对的历史比率" if status == "ok" else "沿用已公开本地历史,来源本次未完整返回" if not incoming and not merged.is_empty() else "部分字段缺少公告原文依据" if not merged.is_empty() else "来源未返回可核对的港股历史报告"
        if conflicts:
            reason = "同一公告版本内容冲突,已保留旧版本"
        if errors:
            reason += ";部分来源请求失败"
        item = {"symbol": symbol, "status": status, "reason": reason,
                "reason_code": "financial_version_conflict" if conflicts else "financial_partial" if status != "ok" else None,
                "source": ",".join(sorted(merged["source"].unique().to_list())) if not merged.is_empty() else None,
                "attempted_sources": attempted, "fallback_used": fallback_rows > 0 or (not incoming and not local.is_empty()),
                "fields_available": available, "fields_missing": sorted(set(HK_FINANCIAL_FIELDS) - set(available)),
                "actual_start": str(merged["period_end"].min()) if not merged.is_empty() else None,
                "actual_end": str(merged["period_end"].max()) if not merged.is_empty() else None,
                "observed_at": datetime.now(UTC).isoformat()}
        item["fallback_used"] = bool(item["fallback_used"])
        if not merged.is_empty():
            staged.append(merged)
        items.append(item)
        if on_progress is not None:
            percent = int((index + 1) * 100 / max(1, len(selected)))
            on_progress("financial_sync", percent, f"港股历史财务同步 {index + 1}/{len(selected)}", percent, True)
    if _hk_financial_job_cancelled(job_id):
        for item in items:
            item.update(status="skipped", reason="任务已取消,未发布财务数据", reason_code="cancelled")
        staged = []
    if staged:
        with _HK_FINANCIAL_WRITE_LOCK:
            # The shared publisher compares this marker under the cross-process
            # claim lock, so a competing update cannot be lost after this read.
            expected_marker = _marker_bytes(data_dir)
            current = get_financial_df(data_dir, "metrics", market="HK")
            merged, conflicts = _merge_hk_report_history(current, *staged)
            conflict_symbols = {item["symbol"] for item in conflicts}
            current_content = {_hk_version_content(row) for row in current.iter_rows(named=True)}
            changed_symbols = {row["symbol"] for row in merged.iter_rows(named=True) if _hk_version_content(row) not in current_content}
            if _hk_financial_job_cancelled(job_id):
                for item in items:
                    item.update(status="skipped", reason="任务已取消,未发布财务数据", reason_code="cancelled")
            elif changed_symbols:
                def before_publish() -> None:
                    if _hk_financial_job_cancelled(job_id):
                        raise RuntimeError("港股财务同步已取消")

                try:
                    _publish_hk_files(
                        data_dir, [(merged, data_dir / "financials" / "metrics" / "hk.parquet")],
                        expected_marker=expected_marker, before_publish=before_publish,
                    )
                except RuntimeError:
                    if not _hk_financial_job_cancelled(job_id):
                        raise
                    for item in items:
                        item.update(status="skipped", reason="任务已取消,未发布财务数据", reason_code="cancelled")
            for item in items:
                if item["symbol"] in conflict_symbols:
                    item.update(status="partial", reason="同步期间公告版本发生冲突,已保留旧版本", reason_code="financial_version_conflict")
                elif item["status"] == "ok" and item["symbol"] not in changed_symbols:
                    item.update(status="unchanged", reason="已核对历史公告,内容无变化")
    succeeded = sum(item["status"] in {"ok", "unchanged"} for item in items)
    skipped = sum(item["status"] == "skipped" for item in items)
    unchanged = sum(item["status"] == "unchanged" for item in items)
    failed = len(items) - succeeded - skipped
    status = "empty" if not selected else "unchanged" if unchanged == len(selected) else "completed" if succeeded == len(selected) else "completed_with_errors" if succeeded or any(item["status"] == "partial" for item in items) else "failed"
    return {"operation": "financial_sync", "status": status, "requested": len(selected), "succeeded": succeeded,
            "failed": failed, "skipped": skipped, "unchanged": unchanged, "items": items,
            "reason": None if succeeded else "未取得完整的新历史财务数据;已保留已有有效版本"}


def _hk_financial_job_cancelled(job_id: str | None) -> bool:
    if job_id is None:
        return False
    from app.services.pipeline_jobs import is_cancelled

    return is_cancelled(job_id)


# ================================================================
# 调度器
# ================================================================

class FinancialScheduler:
    """独立调度器: 每周同步 metrics, 财务表支持手动同步。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._data_dir: Path | None = None
        self._capset: CapabilitySet | None = None
        self._lock = threading.Lock()
        self._last_sync: dict[str, str] = {}  # {table: iso_timestamp}
        # 手动同步(run_now)是否正在进行。前端据此显示"同步中"并防重复点击。
        self._is_syncing = False

    def start(self, data_dir: Path, capset: CapabilitySet, *, auto_schedule: bool = False) -> None:
        """初始化调度器，并按需启动周期同步后台任务。

        auto_schedule=False (默认): 仅初始化 (设置数据目录/能力 + 恢复 last_sync),
            供 /api/financials/sync/* 手动同步使用, 不启动自动调度。
        auto_schedule=True: 额外启动每周一次的 metrics 自动同步 (启动后 60s 首跑)。
        """
        # 先记录 data_dir/capset, 即使当前无 FINANCIAL 也保留引用:
        # 用户稍后在「设置」页升级到 Expert Key 时, update_capabilities() 会把新 capset
        # 推进来,trigger()/run_now() 才能用上 FINANCIAL。否则 _capset 永远是 None,
        # 即便 app.state.capabilities 已更新, 调度器仍报 "no FINANCIAL capability"。
        self._data_dir = data_dir
        self._capset = capset
        if not capset.has(Cap.FINANCIAL) and not _financial_is_custom():
            logger.info("FinancialScheduler skipped: no FINANCIAL capability")
            return
        # 从持久化恢复上次同步时间: 重启后前端仍能显示真实最后同步时间,而非"尚未同步"
        try:
            from app.services import preferences
            restored = dict(preferences.get_financial_sync_times())
            # 老用户迁移兜底: 若某表在 preferences 无记录但 parquet 已存在(升级前同步过),
            # 用 parquet 文件的修改时间作为同步时间并补写持久化。
            for table in FINANCIAL_TABLES:
                if table in restored:
                    continue
                parquet = data_dir / "financials" / table / "part.parquet"
                if parquet.exists():
                    mtime = datetime.fromtimestamp(parquet.stat().st_mtime, tz=UTC).isoformat()
                    restored[table] = mtime
                    preferences.set_financial_sync_time(table, mtime)
                    logger.info("FinancialScheduler backfilled last_sync for %s from parquet mtime", table)
            self._last_sync = restored
            if self._last_sync:
                logger.info("FinancialScheduler restored last_sync: %s", list(self._last_sync.keys()))
        except Exception as e:
            logger.warning("restore financial_sync_times failed: %s", e)

        if not auto_schedule:
            # 仅初始化 (手动同步用), 不启动周期任务。
            logger.info("FinancialScheduler initialized (auto-schedule disabled; manual sync only)")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("FinancialScheduler started (auto-schedule enabled)")

    def _record_sync(self, table: str) -> None:
        """记录一张表的同步完成时间: 更新内存 + 持久化到 preferences.json。

        持久化确保即使重启,前端 /status 仍返回真实的最后同步时间,
        不会错误地显示"尚未同步"。
        """
        ts = datetime.now(UTC).isoformat()
        self._last_sync[table] = ts
        try:
            from app.services import preferences
            preferences.set_financial_sync_time(table, ts)
        except Exception as e:
            logger.warning("persist financial_sync_time(%s) failed: %s", e)

    def update_capabilities(self, capset: CapabilitySet) -> None:
        """刷新调度器持有的能力集。

        用户在「设置」页新增/清除 API Key 后, settings API 会重新探测能力并更新
        app.state.capabilities; 必须同步推给本调度器, 否则 trigger()/run_now() 仍读
        启动时的旧 capset, 即便 app.state 已含 FINANCIAL, 调度器仍报
        "no FINANCIAL capability" 而拒绝同步 (表现为前端「全部同步」按钮闪一下无动作)。
        """
        prev = self._capset
        self._capset = capset
        had = bool(prev) and prev.has(Cap.FINANCIAL)
        now = capset.has(Cap.FINANCIAL)
        if had != now:
            logger.info(
                "FinancialScheduler capabilities updated: FINANCIAL %s -> %s", had, now
            )

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("FinancialScheduler stopped")

    async def _run_loop(self) -> None:
        """每周执行一次 metrics 同步。"""
        try:
            while self._running:
                # 首次启动等 60s, 之后每 7 天执行一次
                await asyncio.sleep(60)
                if not self._running:
                    break

                # 每周: 只同步 metrics
                try:
                    rows = sync_metrics(self._data_dir, self._capset)
                    self._record_sync("metrics")
                    logger.info("FinancialScheduler: metrics synced, %d rows", rows)
                except Exception as e:
                    logger.warning("FinancialScheduler: metrics sync failed: %s", e)

                # 等待下一次 (7天)
                for _ in range(7 * 24 * 60):  # 每分钟检查一次 _running
                    if not self._running:
                        break
                    await asyncio.sleep(60)

        except asyncio.CancelledError:
            pass

    def _run_body(self, table: str | None) -> dict[str, int]:
        """同步逻辑本体(不加锁,假设调用方已持有 _is_syncing)。

        table=None 同步全部财务表;否则只同步指定表。
        每张表完成立即更新 last_sync,让前端轮询 /status 能看到进度递增。
        """
        if table:
            fn = {
                "metrics": sync_metrics,
                "income": sync_income,
                "balance_sheet": sync_balance_sheet,
                "cash_flow": sync_cash_flow,
                "shares": sync_shares,
            }.get(table)
            if not fn:
                return {}
            rows = fn(self._data_dir, self._capset)
            self._record_sync(table)
            return {table: rows}
        # 全部同步
        symbols = _get_symbols(self._data_dir)
        result: dict[str, int] = {}
        for t in FINANCIAL_TABLES:
            result[t] = _sync_history_table_for_symbols(
                t, symbols, self._data_dir, self._capset
            )
            self._record_sync(t)
        _refresh_financials_views(self._data_dir)
        return result

    def run_now(self, table: str | None = None) -> dict[str, int]:
        """同步执行一次同步(阻塞调用线程)。

        ⚠ 全量同步需数分钟,务必在后台线程调用,不要直接在 HTTP 请求线程里阻塞,
        否则请求会长时间 pending 直至被浏览器/代理超时掐断(表现为"点击无反应")。
        HTTP 接口应调用 trigger() 立即返回,再让前端轮询 /status.syncing 看进度。

        用 _is_syncing 标志防并发:若已有同步在进行,本次直接跳过,
        避免重复请求拖慢服务端 / 触发上游限流。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync skipped: already running")
                return {"_skipped": 1}
            self._is_syncing = True
        try:
            return self._run_body(table)
        finally:
            with self._lock:
                self._is_syncing = False

    def trigger(self, table: str | None = None) -> dict[str, int]:
        """触发一次同步(非阻塞,立即返回)。

        在后台线程执行同步体,HTTP 请求无需等待。
        返回 {"started": True/False}:
          - False = 能力不足或已有同步在进行(被防并发跳过)
          - True  = 已在后台开始,前端应轮询 /status.syncing 观察进度

        ⚠ _is_syncing 在此处置 True(持锁),确保 trigger 返回时前端轮询
        /status 已能看到 syncing=True,无竞态窗口;同时防止快速重复点击
        启动多个后台线程。后台线程复用 _run_body 执行真正的同步逻辑。
        """
        if not self._capset or (not self._capset.has(Cap.FINANCIAL) and not _financial_is_custom()):
            return {"started": False, "reason": "no FINANCIAL capability"}
        with self._lock:
            if self._is_syncing:
                logger.info("financial sync trigger skipped: already running")
                return {"started": False, "reason": "already running"}
            # 持锁置位:保证 trigger 返回前 syncing 已为 True
            self._is_syncing = True

        def _bg() -> None:
            try:
                self._run_body(table)
            except Exception as e:
                logger.exception("background financial sync failed: %s", e)
            finally:
                with self._lock:
                    self._is_syncing = False

        t = threading.Thread(target=_bg, name="financial-sync", daemon=True)
        t.start()
        logger.info("financial sync triggered in background: table=%s", table or "all")
        return {"started": True}

    @property
    def is_syncing(self) -> bool:
        """手动同步是否正在进行(供 /status 返回,前端据此显示"同步中")。"""
        with self._lock:
            return self._is_syncing

    @property
    def last_sync(self) -> dict[str, str]:
        return dict(self._last_sync)


# 全局单例
financial_scheduler = FinancialScheduler()
