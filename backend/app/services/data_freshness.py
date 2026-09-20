"""数据新鲜度画像 —— 回答"本地现在有什么数据 / 缺哪一段 / 该拉哪个区间"。

供前端底部状态栏常驻展示。三层信息:
  1. 每个市场 enriched(看板实际消费) 与 raw(原始日K) 的最新日期
  2. 落后天数 + 最新日覆盖完成度 (防"少数标的撑起来的日期")
  3. 缺口区间建议 (供用户选择拉取范围, 而不是盲目全量重跑)

设计约束:
  - 港美股是 per-symbol 分区, 全量扫 2.3w 个 parquet 不现实 → 抽样取众数,
    与 daily_pipeline._h6_latest_distribution 同一口径。
  - 前端 2s 级轮询, 结果按 TTL 缓存, 避免每次都打磁盘。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.markets import get_profile

logger = logging.getLogger(__name__)

# 展示顺序与中文标签
MARKET_ORDER: tuple[str, ...] = ("CN", "HK", "US")
MARKET_LABELS: dict[str, str] = {"CN": "A股", "HK": "港股", "US": "美股"}

# 数据落后容差 (自然日): 覆盖周末与常规节假日, 与 daily_pipeline 的
# _MARKET_DAILY_STALENESS_DAYS 保持一致, 避免两处口径打架。
STALENESS_TOLERANCE_DAYS: dict[str, int] = {"CN": 3, "HK": 4, "US": 3}

# 历史深度下限: 低于该天数说明"只有近期数据", 应提示用户补拉全量历史
MIN_HISTORY_DAYS: dict[str, int] = {"CN": 120, "HK": 60, "US": 60}

# 最新日完成度阈值: 低于此值视为"当日只同步了一部分标的"
PARTIAL_COVERAGE_THRESHOLD = 0.5

_CACHE_TTL = 30.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ── 分区扫描 ──────────────────────────────────────────────────


def _as_plain_date(value: object) -> date | None:
    """datetime 是 date 的子类, 必须先判 datetime 再判 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _date_partitions(base: Path) -> list[date]:
    """per-date 分区目录名 → 日期列表 (A 股 kline_daily / kline_daily_enriched)。"""
    if not base.exists():
        return []
    out: list[date] = []
    for p in base.glob("date=*"):
        raw = p.name.split("=", 1)[1]
        try:
            out.append(date.fromisoformat(raw))
        except ValueError:
            continue
    return sorted(out)


def _sample_symbol_dirs(base: Path, suffix: str, sample: int) -> list[Path]:
    parts = sorted(base.glob(f"symbol=*{suffix}/part.parquet")) if base.exists() else []
    if not parts:
        return []
    step = max(1, len(parts) // max(1, sample))
    return parts[::step][:sample]


def _sample_symbol_range(base: Path, suffix: str, sample: int) -> tuple[Counter[date], date | None]:
    """抽样 per-symbol 分区, 返回 (各分区 max(date) 分布, 抽样中的最早日期)。"""
    counts: Counter[date] = Counter()
    earliest: date | None = None
    for path in _sample_symbol_dirs(base, suffix, sample):
        try:
            row = (
                pl.scan_parquet(path)
                .select(pl.col("date").max().alias("_mx"), pl.col("date").min().alias("_mn"))
                .collect()
                .row(0, named=True)
            )
        except Exception:
            continue
        latest = _as_plain_date(row.get("_mx"))
        if latest is not None:
            counts[latest] += 1
        first = _as_plain_date(row.get("_mn"))
        if first is not None and (earliest is None or first < earliest):
            earliest = first
    return counts, earliest


def _modal_and_coverage(counts: Counter[date]) -> tuple[date | None, float]:
    """众数日期 + 该日期在抽样中的占比 (完成度)。"""
    if not counts:
        return None, 0.0
    modal, n = counts.most_common(1)[0]
    total = sum(counts.values())
    return modal, (n / total if total else 0.0)


# ── 单市场新鲜度 ───────────────────────────────────────────────


def market_freshness(
    data_dir: Path,
    market: str,
    *,
    sample: int = 30,
    today: date | None = None,
) -> dict[str, Any]:
    key = (market or "CN").strip().upper()
    if today is None:
        today = get_profile(key).today()

    tolerance = STALENESS_TOLERANCE_DAYS.get(key, 3)
    min_history = MIN_HISTORY_DAYS.get(key, 60)

    latest: date | None = None
    raw_latest: date | None = None
    earliest: date | None = None
    coverage = 0.0
    symbols: int | None = None

    if key == "CN":
        dates = _date_partitions(data_dir / "kline_daily_enriched")
        if dates:
            latest, earliest = dates[-1], dates[0]
            coverage = 1.0
        raw_dates = _date_partitions(data_dir / "kline_daily")
        if raw_dates:
            raw_latest = raw_dates[-1]
            if earliest is None:
                earliest = raw_dates[0]
        symbols = len(dates)
    else:
        suffix = ".HK" if key == "HK" else ".US"
        counts, earliest = _sample_symbol_range(
            data_dir / "kline_hk_us_enriched", suffix, sample
        )
        latest, coverage = _modal_and_coverage(counts)
        raw_counts, raw_earliest = _sample_symbol_range(
            data_dir / "kline_daily", suffix, sample
        )
        raw_latest, _ = _modal_and_coverage(raw_counts)
        if earliest is None:
            earliest = raw_earliest
        symbols = len(list((data_dir / "kline_hk_us_enriched").glob(f"symbol=*{suffix}/part.parquet"))) if (
            data_dir / "kline_hk_us_enriched"
        ).exists() else None

    stale_days = (today - latest).days if latest else None
    history_days = (today - earliest).days if earliest else None

    history_insufficient = history_days is not None and history_days < min_history

    if latest is None:
        status = "empty"
    elif stale_days is not None and stale_days > tolerance:
        status = "stale"
    elif history_insufficient:
        # 日期不落后但只有薄薄一层历史 → 缺的是"更早的全量", 不是"最近几天"
        status = "shallow"
    elif raw_latest and latest < raw_latest - timedelta(days=1):
        # 原始数据已到位但 enriched 没跟上 → 不是"没拉到", 是"没算"
        status = "behind_raw"
    elif coverage < PARTIAL_COVERAGE_THRESHOLD:
        status = "partial"
    else:
        status = "ok"

    gap: dict[str, Any] | None = None
    if latest is None:
        gap = None  # 全新部署, 交给首次同步流程, 不在这里编造区间
    elif status != "ok":
        if history_insufficient:
            # 历史深度不足时直接建议补一段完整历史, 而不是只补尾部
            start = today - timedelta(days=max(min_history, 365))
            end = today
        elif status == "partial":
            # 日期已到最新, 缺的是"当日还有多少标的没同步" → 补跑当天即可
            start = end = latest
        elif status == "behind_raw" and raw_latest:
            start, end = latest + timedelta(days=1), raw_latest
        else:
            start, end = latest + timedelta(days=1), today
        if end < start:
            end = start
        gap = {
            "from": start.isoformat(),
            "to": end.isoformat(),
            "missing_days": max(1, (end - start).days + 1),
            "reason": status,
        }

    return {
        "market": key,
        "label": MARKET_LABELS.get(key, key),
        "today": today.isoformat(),
        "latest_date": latest.isoformat() if latest else None,
        "raw_latest_date": raw_latest.isoformat() if raw_latest else None,
        "earliest_date": earliest.isoformat() if earliest else None,
        "history_days": history_days,
        "history_insufficient": history_insufficient,
        "stale_days": stale_days,
        "tolerance_days": tolerance,
        "coverage_ratio": round(coverage, 3),
        # A 股按日期分区 (单位是交易日), 港美按标的分区 (单位是标的)
        "coverage_units": symbols,
        "coverage_unit_label": "交易日" if key == "CN" else "标的",
        "status": status,
        "gap": gap,
    }


def get_data_freshness(
    data_dir: Path,
    markets: tuple[str, ...] = MARKET_ORDER,
    *,
    sample: int = 30,
    active_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """全市场新鲜度画像。active_job 由 API 层注入 (job_store 属于调度侧, 不在此依赖)。"""
    # 缓存 key 必须带上 data_dir/markets/sample: 单测用 tmp_path, 生产用真实 data 目录,
    # 共用一个槽位会互相串味。
    cache_key = f"{Path(data_dir).resolve()}|{','.join(markets)}|{sample}"
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and (now - hit[0]) < _CACHE_TTL:
            cached = dict(hit[1])
            cached["active_job"] = active_job
            cached["cached"] = True
            return cached

    items = []
    for m in markets:
        try:
            items.append(market_freshness(data_dir, m, sample=sample))
        except Exception as e:
            logger.warning("freshness: %s 计算失败: %s", m, e)
            items.append({"market": m.upper(), "label": MARKET_LABELS.get(m.upper(), m),
                          "status": "unknown", "error": str(e)})

    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "markets": items,
        "active_job": active_job,
        "cached": False,
    }
    with _cache_lock:
        _cache[cache_key] = (now, {k: v for k, v in payload.items() if k != "active_job"})
    return payload
