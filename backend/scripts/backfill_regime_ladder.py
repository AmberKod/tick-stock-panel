"""手动补算港美 regime + 强度梯队的增量缺口。

背景
----
daily_pipeline 的 regime / strength_ladder 两步受 ``pipeline_regime_enabled``
开关门禁(默认 False, 因首次全量回填较重)。开关关闭期间 enriched 仍在正常落盘,
于是 regime/ladder 表会停在开关关闭那天, 看板上的「市场环境 / 强度梯队」卡片
看起来像"数据不更新", 实际只是没算。

本脚本直接调两个服务的增量函数(与 pipeline 走同一条代码路径), 把
"enriched 有、regime/ladder 没有"的交易日补齐, 不做全量重算。

用法
----
::

    # 先看缺口, 不落盘
    python scripts/backfill_regime_ladder.py --dry-run

    # 只补港股
    python scripts/backfill_regime_ladder.py --market hk

    # 港美都补
    python scripts/backfill_regime_ladder.py --market hk,us
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("backfill_regime_ladder")


def _build_repo():
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore()
    return KlineRepository(store), store.data_dir


def _as_date(value) -> date:
    """polars 的 max() 可能给出 datetime/date/str, 统一成 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _regime_latest_date(data_dir: Path, market: str) -> date | None:
    """regime 表最新日期; 空表返回 None。"""
    from app.services import regime_builder

    df = regime_builder.load_regime_history(data_dir, market=market)
    if df.is_empty() or "date" not in df.columns:
        return None
    return _as_date(df["date"].max())


def _gap_days(
    repo, data_dir: Path, market: str, start: date
) -> tuple[list[str], list[str]]:
    """返回 [start, 今天] 区间内 (regime 缺口, ladder 缺口) 的日期列表。"""
    from app.services import regime_builder, strength_ladder

    lo = start.isoformat()
    enriched = {
        str(d)[:10]
        for d in regime_builder.enriched_date_set(repo, market=market)
        if str(d)[:10] >= lo
    }

    existing = regime_builder.load_regime_history(data_dir, market=market)
    have = (
        {str(d)[:10] for d in existing["date"].to_list()}
        if not existing.is_empty()
        else set()
    )
    regime_gap = sorted(enriched - have)

    ladder_df = strength_ladder.load_strength_ladder_history(data_dir, market)
    ladder_have = (
        {str(d)[:10] for d in ladder_df["date"].to_list()}
        if not ladder_df.is_empty()
        else set()
    )
    ladder_gap = sorted(enriched - ladder_have)

    return regime_gap, ladder_gap


def run(markets: list[str], *, dry_run: bool, backfill_days: int) -> int:
    repo, data_dir = _build_repo()
    logger.info("data_dir=%s", data_dir)

    from app.services import regime_builder, strength_ladder

    total_regime = 0
    total_ladder = 0
    end = date.today()

    for mkt in markets:
        # 起点: regime 表最新日的次日; 空表则只回溯 backfill_days 天。
        # 注意: enriched 有 1970s 以来的全历史, 直接跑增量补差 = 全量回填上千天,
        # 那正是 pipeline_regime_enabled 默认关闭的原因。这里只补"用户看得见的"近期。
        latest = _regime_latest_date(data_dir, mkt)
        start = (
            latest + timedelta(days=1)
            if latest is not None
            else end - timedelta(days=backfill_days)
        )
        if start > end:
            logger.info("[%s] regime 已到 %s, 无需补算", mkt, latest)
            continue

        regime_gap, ladder_gap = _gap_days(repo, data_dir, mkt, start)
        logger.info(
            "[%s] 区间 %s..%s 缺口: regime=%d 天, ladder=%d 天",
            mkt, start, end, len(regime_gap), len(ladder_gap),
        )
        if dry_run:
            continue

        t0 = time.perf_counter()
        new_rows = regime_builder.run_regime_batch(repo, start=start, end=end, market=mkt)
        days = new_rows.height if not new_rows.is_empty() else 0
        if days:
            regime_builder.upsert_regime_history(data_dir, new_rows, market=mkt)
            regime_builder.refresh_phase_labels(data_dir, market=mkt)
        total_regime += days
        logger.info("[%s] regime 新算 %d 行, 耗时 %.1fs", mkt, days, time.perf_counter() - t0)

        t1 = time.perf_counter()
        rows = strength_ladder.compute_strength_ladder_incremental(
            repo, data_dir, market=mkt, max_backfill_days=backfill_days,
        )
        total_ladder += int(rows or 0)
        logger.info("[%s] ladder 新算 %s 行, 耗时 %.1fs", mkt, rows, time.perf_counter() - t1)

    logger.info("完成: regime=%d 行, ladder=%d 行 (dry_run=%s)", total_regime, total_ladder, dry_run)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="补算港美 regime + 强度梯队的近期缺口")
    ap.add_argument("--market", default="hk,us", help="逗号分隔: hk,us (默认都补)")
    ap.add_argument("--days", type=int, default=30, help="空表时的回溯天数 (默认 30)")
    ap.add_argument("--dry-run", action="store_true", help="只打印缺口, 不落盘")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    markets = [m.strip().lower() for m in args.market.split(",") if m.strip()]
    return run(markets, dry_run=args.dry_run, backfill_days=args.days)


if __name__ == "__main__":
    raise SystemExit(main())
