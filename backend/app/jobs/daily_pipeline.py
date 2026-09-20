"""盘后管道 + 盘前维表同步。

调度:
  09:10 盘前 — 同步个股维表 instruments (全量覆盖)
  15:30 盘后 — 日K同步 + 增量除权因子 + enriched 计算 + 刷新视图

盘后同步策略:
  日 K: QuoteService 交易时段已实时落盘 → 有数据时跳过 batch,首次拉 1 年区间
  除权因子: 从已有数据最新日期的下一天开始增量获取,避免重复拉取和计算
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.indicators.pipeline import run_pipeline
from app.services import index_sync, instrument_sync, kline_sync
from app.services import preferences as _prefs
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.pools import DEMO_SYMBOLS, get_pool
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

ProgressCb = Callable[..., None]


class PipelineStageError(RuntimeError):
    """管道有阶段软失败(数据可能陈旧)时抛出, 让上层 job_store 把任务标记为 failed。

    这些阶段单独 try/except 吞掉异常以不中断整条管道, 但一旦失败即代表对应数据陈旧。
    抛出前进度协议已走完(done/100), 故前端进度条正常收尾, 仅终态如实反映为 failed ——
    不再"部分失败却报成功"。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("盘后管道部分阶段失败: " + "; ".join(errors))


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:
    pass


def _invalidate(table: str | None = None) -> None:
    """stage 写完调用,让 /api/data/status 只重算被影响的那张表。"""
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet, repo=None) -> list[str]:
    """解析标的池 — 以 CN_Equity_A (沪深京A股 ~5522只) 为主。

    有 batch 能力 → 直接拉 CN_Equity_A universe
    其他用户 → 用 instruments parquet + watchlist 兜底

    repo 传入时过滤自选兜底里的指数 symbol (指数日K走独立 kline_index_* 存储,
    进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    """
    if capset.has(Cap.KLINE_DAILY_BATCH):
        try:
            all_a = get_pool("CN_Equity_A", refresh=True)
            if all_a:
                return sorted(all_a)
        except Exception as e:
            logger.warning("CN_Equity_A pool unavailable, fallback: %s", e)

    # Free 用户兜底: instruments parquet + watchlist + demo
    base: set[str] = set(DEMO_SYMBOLS)
    base.update(get_pool("watchlist"))
    d = Path(settings.data_dir)
    inst_path = d / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:
            logger.warning("instruments supplement failed: %s", e)
    # 过滤自选兜底里的指数 symbol (指数日K走独立 kline_index_* 存储,
    # 进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    if repo is not None:
        base -= set(repo.get_index_symbol_set())
    return sorted(base)


def run_instruments_sync(repo: KlineRepository) -> dict:
    """盘前同步个股维表。

    维表含当日涨跌停价 (limit_up/down), 同步完成后刷新 enriched 内存缓存,
    确保跨天后连板梯队/选股等读到的是基于最新维表的数据 (而非前一交易日残留)。
    """
    rows = instrument_sync.sync_instruments(repo.store.data_dir)
    _refresh_instruments_view(repo)
    _invalidate("instruments")
    # 维表更新后重建 enriched 缓存 (clear + refresh, 与设置页「清理并刷新」同等效果)
    if rows > 0:
        repo.clear_cache()
        repo.refresh_cache()
    return {"instruments_rows": rows}


def run_now(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
    override_start_date: date | None = None,
) -> dict:
    """立即执行一次盘后管道,支持进度回调。

    跳过的 stage **不 emit**,避免前端把"无 capability"的卡片错误标记为 active/done。
    result 里带 skipped_stages 列表供前端展示。

    override_start_date: 传入时强制走 batch 拉取分支,用该日期作为日K/除权/指数的
        拉取起点(到今天),用于「数据修正/补数据」场景。None 时走原有自动判定逻辑。
    """
    emit = on_progress or _noop
    skipped: list[str] = []
    # 阶段软失败累积: 下列阶段 try/except 吞异常以不中断管道, 但失败即代表数据可能陈旧。
    # 管道末尾若非空则抛 PipelineStageError, 让任务终态如实标记为 failed(而非误报成功)。
    stage_errors: list[str] = []

    # Step 0: 先同步个股维表, 再解析标的池 — 确保标的池基于最新 instruments
    emit("sync_instruments", 2, "同步个股维表…")
    inst_rows = instrument_sync.sync_instruments(repo.store.data_dir)
    if inst_rows > 0:
        _refresh_instruments_view(repo)
    emit("sync_instruments", 8, f"个股维表同步完成,{inst_rows} 只标的")
    _invalidate("instruments")

    emit("resolve_universe", 9, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    # Step 1: 日 K 同步
    #   override_start_date 传入 → 强制 batch 拉取 [override_start_date ~ today] (数据修正)
    #   付费档 + 今天有数据 → 实时行情接口拉一次覆写（1请求全市场）
    #   有历史数据 → batch K-line API 补齐缺口
    #   无任何数据 → batch K-line API 拉首次 1 年
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    latest_daily = repo.latest_daily_date()
    today = _date.today()
    today_exists = latest_daily and latest_daily >= today
    new_daily_days = 0

    # 完整性自愈: 检测最近交易日的盘中快照/缺口 (盘中停机后次日开实时会留下
    # 中午快照, 而下方"今天已有数据→只刷今天"分支会让它永久留存)。
    # 命中 → 本次管道放弃实时覆写分支, 降级为从最早坏日起的范围拉取。
    integrity_issues: list = []
    stale_day: _date | None = None
    etf_stale_day: _date | None = None
    index_stale_day: _date | None = None
    if override_start_date is None:
        try:
            from app.services import data_integrity
            integrity_issues = data_integrity.scan_recent_integrity(
                repo.store.data_dir, today=today,
            )
            if integrity_issues:
                stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_daily",))
                etf_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_etf_daily",))
                index_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_index_daily",))
                logger.warning(
                    "integrity: 检测到 %d 个不完整分区(%s), 本次管道改走范围拉取修复",
                    len(integrity_issues), data_integrity.describe_issues(integrity_issues),
                )
        except Exception as e:
            logger.warning("integrity scan failed (soft, 按无坏数据处理): %s", e)
            integrity_issues = []
    # 日K范围拉取的起点(分支3补缺口/分支4首次/数据修正); 实时增量/跳过时为 None。
    # 供 Step 1.5 除权因子回溯范围对齐: 范围拉取→用日K范围, 非范围→最近N天兜底。
    daily_range_start: _date | None = None

    # A 股日K拉取开关(默认开);关闭时跳过日K同步,保留已有数据。
    # 数据修正(override_start_date)时即使关闭开关也强制拉取 — 修正就是来补数据的。
    pull_a_share = _prefs.get_pipeline_pull_a_share()
    if not pull_a_share and not override_start_date:
        emit("sync_daily", 45, "已跳过 A 股日K同步(拉取内容未勾选)")
        logger.info("sync_daily: skipped (pipeline_pull_a_share=False)")
    elif override_start_date:
        # 数据修正: 强制用传入日期作起点 batch 拉取, 忽略实时行情覆写分支。
        start_date = override_start_date
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] repair/override", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    elif (
        today_exists
        and stale_day is None
        and capset.has(Cap.QUOTE_POOL)
        and _prefs.get_daily_data_provider() == "tickflow"
    ):
        # 付费档:今天有数据(QuoteService 已落盘)→ 实时行情覆写,确保最新。
        # stale_day 非空时禁用本分支: "只刷今天"会让停机日的盘中快照永久留存,
        # 降级到下方 batch 路径从坏日起重拉。
        # free/none 档无 quote.pool 能力,即便今天已有数据(如从 expert 降级),
        # 也降级到下方 batch 路径刷新,避免调用无权限的实时行情接口。
        emit("sync_daily", 12, f"获取日K [{today} ~ {today}] 实时行情…")
        written_daily = kline_sync.sync_daily_by_quotes(repo)
        new_daily_days = 1
        emit("sync_daily", 45, f"日K 完成,{written_daily} 只标的")
        logger.info("sync_daily: [%s ~ %s] live quotes, %d symbols", today, today, written_daily)
    elif latest_daily or stale_day:
        # 有历史 → batch 补齐缺口。
        # 也覆盖"今天已有数据但无实时行情权限(free/none)"的降级场景:
        #   此时 start_date = latest_daily = today,batch 刷新当天日K。
        # 完整性修复场景: start_date = min(本地最新日, 最早坏日) —
        #   today_exists 时 latest_daily=今天, 不取 min 会漏掉坏日。
        start_date = min(d for d in (latest_daily, stale_day) if d is not None)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] %s", start_date, today,
                    "refresh today" if today_exists else "gap fill")

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    else:
        # 首次：无任何数据 → batch 拉 1 年
        start_date = today - _td(days=365)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] initial fetch", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        new_daily_days = 365
        emit("sync_daily", 45, "日K 完成")
        logger.info("sync_daily: [%s ~ %s] done", start_date, today)
    _invalidate("daily")

    # 完整性修复时删除股票 enriched 的坏分区: 增量重算只算 enriched 里不存在
    # 的日期, 盘中快照日分区已存在(虽是错的), 不删永远不会被重算。删除后
    # Step 2 把这些日期当"新日期"重算 (剩余分区最近 60 天做历史前缀, 窗口 ≤5 天回看充足)。
    repair_start = override_start_date if override_start_date is not None else stale_day
    if repair_start is not None:
        try:
            from app.services.data_integrity import prune_enriched_partitions
            pruned = prune_enriched_partitions(
                repo.store.data_dir, repair_start, "kline_daily_enriched",
            )
            if pruned:
                logger.info("integrity: 已删除 %d 个待重算的 enriched 分区 (≥ %s)", pruned, repair_start)
        except Exception as e:
            logger.warning("enriched prune failed (soft): %s", e)


    # 单标的新鲜度: 全局 max(date) 会被任一有今日数据的标的"拉高", 掩盖停牌/复牌/
    # 一直拉失败而掉队的个股缺口(全局判据只刷"今天", 永不回补掉队标的的历史缺口)。
    # 这里检测并**可见化**(WARNING + 计入结果), 让掉队标的不再隐形。
    # (自动回补暂不做 —— 需带退市判定, 否则对已退市标的每轮空拉浪费 API 额度。)
    lagging_symbols: list[str] = []
    if pull_a_share and latest_daily:
        try:
            lagging_symbols = repo.symbols_lagging(today, min_gap_days=3)
            if lagging_symbols:
                logger.warning("日K新鲜度: %d 只标的落后 >3 日 (停牌/退市/拉取失败; 样例: %s)",
                               len(lagging_symbols), lagging_symbols[:10])
        except Exception as e:
            logger.warning("laggard detection failed: %s", e)
            stage_errors.append(f"laggard detection: {e}")

    # Step 1.5: 同步除权因子 — 范围与日K拉取方式对齐
    #   日K范围拉取(补缺口/首次) → 除权用日K范围 [daily_range_start, now]
    #     首次会覆盖整个日K区间内的历史除权事件; 补缺口天然只增量(起点=latest_daily≈昨天)
    #   日K实时增量/跳过(分支2/分支1) → 除权兜底拉最近 30 天, 补可能遗漏的新除权
    #     (这两类分支不拉历史日K, 除权不能用日K范围, 只能兜底最近几日)
    affected_symbols: list[str] = []
    adj_provider = _prefs.get_adj_factor_provider()
    if adj_provider == "same_as_daily":
        adj_provider = _prefs.get_daily_data_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        from datetime import datetime, timedelta
        adj_end = datetime.now()
        if daily_range_start is not None:
            adj_start = datetime.combine(daily_range_start, datetime.min.time())
        else:
            # 日K实时增量/跳过时, 除权兜底拉最近 N 天, 覆盖周末/长假/停机期间的新除权事件。
            # 15 天: 覆盖春节/国庆最长约10天长假 + 故障恢复缓冲; sync_adj_factor 内部 merge+unique 幂等, 多拉无副作用。
            adj_start = adj_end - timedelta(days=15)
        adj_start_str = adj_start.strftime("%Y-%m-%d")
        adj_end_str = adj_end.strftime("%Y-%m-%d")
        emit("sync_adj", 50, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("sync_adj: [%s ~ %s] start", adj_start_str, adj_end_str)

        def _adj_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_adj", 50 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        _written_adj, affected_symbols = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk_progress,
        )
        if affected_symbols:
            _refresh_single_view(repo, "adj_factor")
            emit("sync_adj", 60, f"除权因子完成,新增 {len(affected_symbols)} 只个股")
            logger.info("sync_adj: [%s ~ %s] done, %d symbols", adj_start_str, adj_end_str, len(affected_symbols))
        else:
            emit("sync_adj", 60, "除权因子完成,无新增")
            logger.info("sync_adj: [%s ~ %s] no new factors", adj_start_str, adj_end_str)
        _invalidate("adj_factor")
    else:
        skipped.append("sync_adj")
        logger.info("sync_adj skipped: no ADJ_FACTOR capability")

    # Step 2: 计算 enriched
    #   判断策略:
    #     - 首次 (enriched 目录不存在) → 全量
    #     - 往前扩展历史 (新日期 < enriched 已有最早日期) → 全量
    #       前面的除权因子会改变累积因子链,影响后面所有日期的复权价格
    #     - 往后新增日期 (新日期 > enriched 已有最晚日期)
    #       → 增量补新区块(所有标的) + 受除权影响个股全日期重算
    #     - 无新日期 + 有新除权因子 → 增量: 只重算受影响个股的全部日期
    #     - 无新日期 + 无变化 → 跳过
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0
    prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 判断新日期方向: 找 daily 和 enriched 的日期集合做比较
    forward_incremental = False
    backward_extension = False

    if daily_days > prev_enriched_days and enriched_exists:
        daily_dates = sorted(d.stem.split("=")[1] for d in daily_dir.glob("date=*"))
        enriched_dates = sorted(d.stem.split("=")[1] for d in enriched_dir.glob("date=*"))
        earliest_enriched = enriched_dates[0]
        latest_enriched = enriched_dates[-1]
        new_dates = set(daily_dates) - set(enriched_dates)
        if new_dates:
            # 有新日期早于 enriched 最早日期 → 往前扩展
            if any(d < earliest_enriched for d in new_dates):
                backward_extension = True
            # 有新日期晚于 enriched 最晚日期 → 往后新增
            if any(d > latest_enriched for d in new_dates):
                forward_incremental = True

    def _enriched_batch_progress(cur: int, tot: int) -> None:
        emit("compute_enriched", 65 + int(23 * cur / tot),
             f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    if not enriched_exists or backward_extension:
        # 首次 或 往前扩展 → 全量
        emit("compute_enriched", 65, "全量计算 enriched…")
        logger.info("compute_enriched: full rebuild (first=%s, backward=%s, daily=%d, enriched=%d)",
                    not enriched_exists, backward_extension, daily_days, prev_enriched_days)
        written_enriched = run_pipeline(on_batch_done=_enriched_batch_progress)
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: full rebuild done, %d days", new_enriched_days)
    elif forward_incremental:
        # 往后新增日期: 增量补新区块 + 受影响个股全日期重算
        symbols_to_recompute = list(set(affected_symbols)) if affected_symbols else []
        emit("compute_enriched", 65,
             f"增量计算 enriched (新日期 + {len(symbols_to_recompute)} 只个股重算)…"
             if symbols_to_recompute else "增量计算 enriched (新日期)…")
        logger.info("compute_enriched: forward incremental, %d symbols to recompute",
                    len(symbols_to_recompute))
        written_enriched = run_pipeline(
            new_dates_only=True,
            symbols=symbols_to_recompute or None,
            on_batch_done=_enriched_batch_progress,
        )
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: forward incremental done, %d days", new_enriched_days)
    elif affected_symbols:
        # 无新日期,仅除权因子变更 → 只重算受影响个股的全部日期
        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected_symbols)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental, %d symbols", len(affected_symbols))
        written_enriched = run_pipeline(symbols=affected_symbols, on_batch_done=_enriched_batch_progress)
        emit("compute_enriched", 88, f"enriched 完成,{len(affected_symbols)} 只个股")
    else:
        written_enriched = 0
        logger.info("compute_enriched: skip (no new daily, no adj_factor changes)")
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # Step 2.3: 指数 / ETF 同步 — 物理分开存储；ETF 可复权，指数不复权。
    written_index_daily = 0
    written_etf_daily = 0
    index_count = 0
    etf_count = 0
    etf_adj_symbols = 0
    pull_index = _prefs.get_pipeline_pull_index()
    pull_etf = _prefs.get_pipeline_pull_etf()

    if capset.has(Cap.KLINE_DAILY_BATCH) and (pull_index or pull_etf):
        _types = []
        if pull_index:
            _types.append("指数")
        if pull_etf:
            _types.append("ETF")
        emit("sync_index", 88, f"同步{'+'.join(_types)}日K…")
        # 子阶段进度分配: 88.0(开始) → 89.0(完成), 指数占前半, ETF 占后半
        try:
            if pull_index:
                emit("sync_index", 88, "同步指数维表…")
                index_count = index_sync.sync_index_instruments(repo, pull_index=True, pull_etf=False)
                emit("sync_index", 88, f"指数维表完成,{index_count} 只")
                index_dir = repo.store.data_dir / "kline_index_enriched"
                index_dates = sorted(
                    d.name[5:] for d in index_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if index_dir.exists() else []
                # 数据修正模式下用传入起点; 否则用本地指数最新日期补到今天;
                # 完整性修复时起点再提前到最早坏日 (实时写过今天的指数分区时,
                # "最新日期=今天"会让停机日的快照/缺口永久留存)
                if override_start_date:
                    index_start = override_start_date
                else:
                    index_start = _date.fromisoformat(index_dates[-1]) if index_dates else today - _td(days=365)
                if index_stale_day is not None and index_start > index_stale_day:
                    index_start = index_stale_day

                def _index_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"指数日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_index_daily = index_sync.sync_and_persist_index_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(index_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_index_chunk,
                )
                emit("sync_index", 88, f"指数日K完成,{written_index_daily} 行")
                _invalidate("index_instruments")
                _invalidate("index_daily")
                _invalidate("index_enriched")

            if pull_etf:
                emit("sync_index", 88, "同步 ETF 维表…")
                etf_count = index_sync.sync_etf_instruments(repo)
                emit("sync_index", 88, f"ETF 维表完成,{etf_count} 只")
                etf_symbols: list[str] = []
                etf_inst = repo.get_etf_instruments()
                if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                    etf_symbols = sorted(set(etf_inst["symbol"].to_list()))
                if etf_symbols and capset.has(Cap.ADJ_FACTOR):
                    try:
                        emit("sync_index", 88, "同步 ETF 除权因子…")
                        from datetime import datetime, timedelta
                        adj_end = datetime.now()
                        adj_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
                        fallback_start = adj_end - timedelta(days=30)
                        adj_start = fallback_start
                        if adj_path.exists():
                            max_date = pl.scan_parquet(adj_path).select(pl.col("trade_date").max()).collect().item()
                            if max_date is not None:
                                if isinstance(max_date, str):
                                    adj_start = datetime.combine(_date.fromisoformat(max_date), datetime.min.time())
                                elif isinstance(max_date, datetime):
                                    adj_start = datetime.combine(max_date.date(), datetime.min.time())
                                else:
                                    adj_start = datetime.combine(max_date, datetime.min.time())
                        _, affected_etfs = index_sync.sync_etf_adj_factor(
                            etf_symbols,
                            repo,
                            capset,
                            start_time=adj_start,
                            end_time=adj_end,
                        )
                        etf_adj_symbols = len(affected_etfs)
                        emit("sync_index", 88, f"ETF 除权因子完成,{etf_adj_symbols} 只")
                    except Exception as e:
                        logger.warning("ETF adj_factor skipped: %s", e)
                        stage_errors.append(f"ETF adj_factor: {e}")
                etf_dir = repo.store.data_dir / "kline_etf_enriched"
                etf_dates = sorted(
                    d.name[5:] for d in etf_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if etf_dir.exists() else []
                etf_start = _date.fromisoformat(etf_dates[-1]) if etf_dates else today - _td(days=365)
                # 同指数: 完整性修复时把 ETF 起点提前到最早坏日
                if etf_stale_day is not None and etf_start > etf_stale_day:
                    etf_start = etf_stale_day

                def _etf_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"ETF 日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_etf_daily = index_sync.sync_and_persist_etf_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(etf_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_etf_chunk,
                )
                emit("sync_index", 88, f"ETF 日K完成,{written_etf_daily} 行")
                _invalidate("etf_instruments")
                _invalidate("etf_daily")

            repo.refresh_index_views()
            emit(
                "sync_index",
                89,
                f"同步完成,指数 {index_count} 只/{written_index_daily} 行, ETF {etf_count} 只/{written_etf_daily} 行"
                + (f", ETF复权 {etf_adj_symbols} 只" if etf_adj_symbols else ""),
            )
        except Exception as e:
            logger.warning("sync_index/etf failed: %s", e)
            emit("sync_index", 89, f"指数/ETF同步失败:{e}")
            stage_errors.append(f"index/etf sync: {e}")
    else:
        skipped.append("sync_index")

    # Step 2.5: 分钟 K 同步(可选) — 未启用或无 capability 时静默跳过(不 emit)
    from app.services import preferences
    minute_on = preferences.get_minute_sync_enabled()
    minute_days = preferences.get_minute_sync_days()
    written_minute = 0
    if minute_on and capset.has(Cap.KLINE_MINUTE_BATCH):
        minute_start = today - _td(days=minute_days)
        emit("sync_minute", 90, f"获取分钟K [{minute_start} ~ {today}]…")
        logger.info("sync_minute: [%s ~ %s] start", minute_start, today)
        minute_symbols = _resolve_minute_symbols(capset, repo)
        def _minute_chunk_progress(cur: int, tot: int, seg_label: str = "") -> None:
            emit("sync_minute", 90 + int(3 * cur / tot),
                 f"分钟K 批次 {cur}/{tot}" + (f" [{seg_label}]" if seg_label else ""),
                 stage_pct=int(100 * cur / tot), skip_log=True)
        written_minute = kline_sync.sync_and_persist_minute(
            minute_symbols, repo, capset, days=minute_days,
            on_chunk_done=_minute_chunk_progress,
        )
        minute_dir = repo.store.data_dir / "kline_minute"
        minute_cover_days = len(list(minute_dir.glob("date=*"))) if minute_dir.exists() else 0
        emit("sync_minute", 93, f"分钟K完成,覆盖 {minute_cover_days} 天")
        logger.info("sync_minute: [%s ~ %s] done, %d days", minute_start, today, minute_cover_days)
        _invalidate("minute")
    else:
        skipped.append("sync_minute")
        if minute_on:
            logger.info("sync_minute skipped: no KLINE_MINUTE_BATCH capability")
        else:
            logger.info("sync_minute skipped: user disabled")

    # Step 2.6: 市场环境(regime) 增量计算 — enriched 已就绪后聚合环境指标。
    # 双检测(缺口+stale), 自动补算遗漏/被覆写的日。软失败: 不阻断主管道。
    # 默认关闭: regime 是本地聚合计算(非拉取), 首次/regime 表为空时需全量回填
    # 多日, 内存与耗时较高。用户可在数据页「市场环境」卡片设置里开启自动计算,
    # 或直接在该页面点「重算」手动触发(不受此开关影响)。
    #
    # 多市场路由(commit ④ 治本): 抽到 _compute_regime_step 函数, 内部按 (cn, hk?, us?) 循环。
    # - cn: 永久启用
    # - hk/us: 仅当 instruments/{hk,us}_instruments.parquet 存在(说明 universe 同步过)
    # 软失败单市场: 某一市场失败不影响其他市场继续。
    regime_days = 0
    from app.services import preferences as _prefs_regime
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("regime")
        skipped.append("mainline")
        logger.info("compute_regime/mainline skipped: user disabled (pipeline_regime_enabled=False)")
    else:
        regime_days = _compute_regime_step(
            repo=repo, emit=emit,
            skipped=skipped, stage_errors=stage_errors,
        )

    # Step 2.7: 市场主线(概念/行业涨停梯队聚合) 增量计算 — regime 同开关。
    # 只窄扫连板 >=1 的行, 增量通常 1 天, 开销可忽略。软失败: 不阻断主管道。
    if not _prefs_regime.get_pipeline_regime_enabled():
        # 已在 regime 分支统一 skipped
        mainline_rows = 0
    else:
        mainline_rows = _compute_mainline_step(
            repo=repo, emit=emit,
            skipped=skipped, stage_errors=stage_errors,
        )

    # Step 2.8: 强度梯队(港美动量档位) 增量补算 — regime 同开关。
    # 港美无连板梯队, 用动量档位替代; A 股不适用, 由 step 内部跳过。
    # 补算的是"enriched 有但 ladder 没有"的交易日, 最多回溯 30 天。软失败。
    ladder_rows = 0
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("strength_ladder")
    else:
        ladder_rows = _compute_strength_ladder_step(
            repo=repo, emit=emit,
            skipped=skipped, stage_errors=stage_errors,
        )

    # Step 3: 刷新视图
    emit("refresh_views", 95, "刷新 DuckDB 视图…")
    _refresh_views(repo)

    emit("done", 100, "完成")
    _invalidate(None)  # 兜底:全清

    result = {
        "universe_size": len(universe),
        "daily_days": new_daily_days,
        "adj_factor_symbols": len(affected_symbols),
        "enriched_days": written_enriched,
        "index_count": index_count,
        "index_daily_rows": written_index_daily,
        "etf_count": etf_count,
        "etf_daily_rows": written_etf_daily,
        "etf_adj_factor_symbols": etf_adj_symbols,
        "minute_rows": written_minute,
        "regime_days": regime_days,
        "mainline_rows": mainline_rows,
        "strength_ladder_rows": ladder_rows,
        "lagging_symbols": len(lagging_symbols),
        "integrity_repair_from": repair_start.isoformat() if repair_start else None,
        "integrity_issues": len(integrity_issues),
        "skipped_stages": skipped,
        "stage_errors": stage_errors,
    }

    # 有阶段软失败: 进度协议已走完(done/100, 前端进度条正常收尾), 但数据可能陈旧,
    # 抛出让上层 job_store 把终态标记为 failed —— 不再"部分失败却报成功"。
    if stage_errors:
        raise PipelineStageError(stage_errors)

    return result


def _compute_regime_step(*, repo, emit, skipped: list, stage_errors: list) -> int:
    """执行 regime 增量计算的子步骤(commit ④ 治本: 抽成独立函数以便测试)。

    行为契约:
    - 启用市场列表: cn 永久; hk/us 视 instruments/{hk,us}_instruments.parquet 存在而启用。
    - 调 compute_regime_incremental(repo, data_dir, market=mkt), 默认 market='cn' 兼容老调用。
    - 单市场失败: 软失败, 不影响其他市场继续。
    - 日志: 每个市场独立行 compute_regime[market] (软失败时含失败原因)。
    - 阶段切换推送: 按 market 独立调 _push_phase_change_alert(market=mkt)。
    - 缓存: 任一市场有新数据 → 调 invalidate_regime_cache(全局缓存, 安全)。
    - 返回: 跨所有启用市场新算出的总天数(供 result 字段统计)。
    """
    from pathlib import Path

    from app.api.regime import invalidate_regime_cache
    from app.services import regime_builder

    data_dir_root = repo.store.data_dir
    # 决定启用的市场列表
    enabled_markets: list[str] = ["cn"]
    for mkt, fname in (("hk", "hk_instruments.parquet"), ("us", "us_instruments.parquet")):
        if (Path(data_dir_root) / "instruments" / fname).exists():
            enabled_markets.append(mkt)

    total_days = 0
    for mkt in enabled_markets:
        try:
            emit("compute_regime", 90, f"计算市场环境[{mkt}]…")
            new_regime = regime_builder.compute_regime_incremental(
                repo, data_dir_root, market=mkt,
            )
            days = new_regime.height if not new_regime.is_empty() else 0
            total_days += days
            if days:
                invalidate_regime_cache()
                logger.info("compute_regime[%s]: %d days", mkt, days)
            emit("compute_regime", 92, f"市场环境[{mkt}] {days} 天")
            # 阶段切换推送监控通知 (软失败, 不影响管道): 末两日阶段不同 = 今日发生切换。
            # 切入退潮/冰点为风险信号, 用 warn 级别; 其余 info。
            if days:
                try:
                    _push_phase_change_alert(data_dir_root, market=mkt)
                except Exception as e:
                    logger.warning("phase change alert failed (soft, market=%s): %s", mkt, e)
        except Exception as e:
            logger.warning("compute_regime[%s] failed (soft): %s", mkt, e)
            stage_errors.append(f"compute_regime[{mkt}]: {e}")
            skipped.append(f"regime[{mkt}]")
    return total_days


def _compute_mainline_step(*, repo, emit, skipped: list, stage_errors: list) -> int:
    """市场主线(概念/行业涨停梯队聚合) 增量计算 — 只窄扫连板 >=1 的行, 增量通常 1 天。"""
    mainline_rows = 0
    try:
        emit("compute_mainline", 93, "计算市场主线…")
        from app.services import market_mainline
        for _kind in ("concept", "industry"):
            rows = market_mainline.compute_mainline_incremental(
                repo, repo.store.data_dir, kind=_kind
            )
            mainline_rows += rows.height if not rows.is_empty() else 0
        if mainline_rows:
            logger.info("compute_mainline: %d rows", mainline_rows)
        emit("compute_mainline", 94, f"市场主线 {mainline_rows} 行")
    except Exception as e:
        logger.warning("compute_mainline failed (soft): %s", e)
        stage_errors.append(f"compute_mainline: {e}")
        skipped.append("mainline")
    return mainline_rows


def _compute_strength_ladder_step(*, repo, emit, skipped: list, stage_errors: list) -> int:
    """强度梯队(港美动量档位) 增量补算 — 与 regime 同开关, 按市场循环。

    - cn 跳过: A 股走连板梯队(由 market_phase / monitor / depth_service 协同),
      梯队服务本身也拒绝 cn。
    - hk/us: 仅当 instruments/{hk,us}_instruments.parquet 存在才启用, 与
      _compute_regime_step 的市场启用判定保持一致。
    - 软失败: 单市场失败不影响其他市场, 也不阻断主管道。
    """
    ladder_rows = 0
    from app.services import strength_ladder

    data_dir = repo.store.data_dir
    enabled_markets = [
        mkt for mkt, fname in (("hk", "hk_instruments.parquet"), ("us", "us_instruments.parquet"))
        if (Path(data_dir) / "instruments" / fname).exists()
    ]
    if not enabled_markets:
        skipped.append("strength_ladder")
        logger.info("compute_strength_ladder skipped: 港美 universe 未同步")
        return 0

    for mkt in enabled_markets:
        try:
            emit("compute_strength_ladder", 94, f"计算强度梯队[{mkt}]…")
            rows = strength_ladder.compute_strength_ladder_incremental(
                repo, data_dir, market=mkt,
            )
            ladder_rows += rows
            if rows:
                logger.info("compute_strength_ladder[%s]: %d rows", mkt, rows)
            emit("compute_strength_ladder", 95, f"强度梯队[{mkt}] {rows} 行")
        except Exception as e:
            logger.warning("compute_strength_ladder[%s] failed (soft): %s", mkt, e)
            stage_errors.append(f"compute_strength_ladder[{mkt}]: {e}")
            skipped.append(f"strength_ladder[{mkt}]")
    return ladder_rows


def _refresh_views(repo: KlineRepository) -> None:
    """刷新所有 DuckDB 视图 —— 委托给 repository 的唯一权威实现 rebuild_views()。"""
    repo.rebuild_views()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{path}', union_by_name=true)"
        )
    except Exception as e:
        logger.warning("refresh view %s failed: %s", name, e)


def _resolve_minute_symbols(capset: CapabilitySet, repo=None) -> list[str]:
    """分钟 K 同步标的 — 与日K共用同一标的池。"""
    return _resolve_universe(capset, repo)


def _refresh_instruments_view(repo: KlineRepository) -> None:
    """单独刷新 instruments 视图。"""
    d = repo.store.data_dir.as_posix()
    try:
        repo.db.execute(
            f"CREATE OR REPLACE VIEW instruments AS "
            f"SELECT * FROM read_parquet('{d}/instruments/**/*.parquet', union_by_name=true)"
        )
    except Exception as e:
        logger.warning("refresh instruments view failed: %s", e)


def _push_phase_change_alert(data_dir, market: str = "cn") -> None:
    """情绪周期阶段切换 → 推送监控通知(SSE toast + 监控中心)。

    阶段切换(如 退潮→冰点)是重要的市场信号, 原先只有打开市场环境页才能看到。
    复用 quote_service.push_alerts 广播通道; 未发生切换静默返回。
    market: cn/hk/us — commit ④ 扩展, 各市场独立推送自身阶段切换。
    """
    from app.services.market_phase import PHASE_LABELS
    from app.services.regime_builder import latest_phase_transition

    tr = latest_phase_transition(data_dir, market=market)
    if not tr:
        return
    prev, cur, d = tr
    market_tag = "" if market == "cn" else f"[{market.upper()}] "
    msg = f"情绪周期阶段切换: {market_tag}{PHASE_LABELS.get(prev, prev)} → {PHASE_LABELS.get(cur, cur)} ({d})"
    severity = "warn" if cur in ("ebb", "ice") else "info"
    app_state = _get_app_state()
    qs = getattr(app_state, "quote_service", None) if app_state else None
    if qs:
        qs.push_alerts([{
            "source": "market",
            "type": "phase_change",
            "message": msg,
            "severity": severity,
        }])
    logger.info("phase change alert: %s (severity=%s)", msg, severity)


def _run_tracked(fn, job_label: str) -> bool:
    """调度触发时包装 JobStore 跟踪，确保同步历史有记录。

    单飞: 若已有活跃(pending∨running)任务(手动同步中), 本次调度直接跳过, 不并发。
    重任务执行槽: 再挡一层僵尸并发(reap 后线程仍活时不得并行写 parquet)。
    返回 True 仅表示任务已成功并且执行槽已释放。
    """
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create()
    if not is_new:
        logger.info("scheduled %s 跳过: 已有活跃任务在运行 (job_id=%s)", job_label, job_id)
        return False
    if not try_acquire_run_slot(job_id):
        logger.warning("scheduled %s 跳过: 重任务执行槽被占用(疑似上次任务卡死)", job_label)
        job_store.fail(job_id, f"scheduled {job_label} skipped: 已有数据任务在运行")
        return False

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    succeeded = False
    try:
        job_store.start(job_id)
        result = fn(on_progress=progress)
        job_store.succeed(job_id, result)
        succeeded = True
        logger.info("scheduled %s completed: job_id=%s", job_label, job_id)
    except JobCancelledError:
        # 已由 terminate() 标记失败(卡死/手动取消), 拉取线程在分块回调处自行退出
        logger.warning("scheduled %s cancelled: job_id=%s", job_label, job_id)
    except Exception:
        logger.exception("scheduled %s failed: job_id=%s", job_label, job_id)
        job_store.fail(job_id, f"scheduled {job_label} failed")
    finally:
        release_run_slot(job_id)
    return succeeded


def run_pipeline_then_refresh(
    repo: KlineRepository, capset: CapabilitySet, on_progress=None,
) -> dict:
    """盘后管道 + 缓存刷新: 调度(15:30) 与启动 catch-up 共用同一条路径。

    - 与手动触发 (/api/pipeline/run) 对齐: 管道落盘后重建 Polars 内存缓存,
      否则 live_agg 的昨日连板数等基准列会停留在旧交易日, 次日开盘连板梯队
      整体少算一档 (仅手动触发或重启才会刷缓存, cron 调度路径此前漏了这步)。
    - 用 app.state 上的**实时** capset(周期重探会热更新它), 而非启动时捕获的
      旧 capset —— 否则 Key 中途过期/续费后, 调度管道仍按旧档位打端点。
    """
    app_state = _get_app_state()
    capset_live = getattr(app_state, "capabilities", None) or capset
    # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
    qs = getattr(app_state, "quote_service", None)
    try:
        if qs:
            with qs.paused():
                result = run_now(repo, capset_live, on_progress=on_progress)
        else:
            result = run_now(repo, capset_live, on_progress=on_progress)
    finally:
        # 即便有阶段软失败(run_now 末尾抛 PipelineStageError), 已落盘的日K/enriched
        # 仍需刷进内存缓存, 否则 live_agg 基准列停留在旧交易日。放 finally 保证部分
        # 成功也生效; 随后异常继续上抛, 由 _run_tracked 标记任务 failed。
        repo.refresh_cache()
    return result


def _scheduled_pipeline_task(pipeline_fn) -> None:
    """Run weekly mining only after the tracked daily pipeline has fully succeeded."""
    if not _run_tracked(pipeline_fn, "daily_pipeline"):
        return
    try:
        from app.services.mining_schedule import run_weekly_mining

        result = run_weekly_mining(_get_app_state())
        logger.info("scheduled mining result: %s", result)
    except Exception:
        logger.exception("scheduled mining enqueue failed; daily pipeline remains succeeded")


# ================================================================
# 定时复盘 (AI 大盘复盘报告)
# ================================================================

REVIEW_JOB_ID = "scheduled_review"


async def _run_scheduled_review(repo) -> None:
    """定时复盘 job: 流式生成复盘 → 实时推 SSE(开着页面可见) → 落盘归档 → 推飞书。

    与手动「生成复盘」体验一致: 流式事件经 quote_service.push_review_event →
    /api/intraday/stream 的 review_progress 事件 → 前端 reviewStore, 用户开着复盘页
    即可看到报告边生成边显示, 切走再回来也能看到生成中/已生成。
    LLM 偶发断流(peer closed connection)时自动重试最多 2 次。
    任何异常都吞掉只记日志, 绝不影响调度器主循环。
    """
    import json

    try:
        from app import secrets_store as ss
        from app.services import market_recap_reports

        # AI Key 未配置时跳过(避免每日报错刷日志)
        if not ss.get_ai_key():
            logger.info("scheduled review skipped: AI key not configured")
            return

        app_state = _get_app_state()
        quote_service = getattr(app_state, "quote_service", None) if app_state else None
        depth_service = getattr(app_state, "depth_service", None) if app_state else None

        content, meta = await _stream_review_with_retry(repo, quote_service, depth_service)
        if not content:
            logger.warning("scheduled review produced no content (meta=%s)", meta)
            # 通知前端进入 error 态(若有页面在听)
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "error", "message": "复盘生成失败,请稍后手动重试"},
                    ensure_ascii=False))
            return

        # 落盘: 与手动生成完全相同的归档格式
        market_recap_reports.save_report({
            "as_of": meta.get("as_of"),
            "focus": "",
            "content": content,
            "summary": meta.get("summary", ""),
            "emotion_score": meta.get("emotion_score"),
            "emotion_label": meta.get("emotion_label", ""),
        })
        logger.info("scheduled review saved: as_of=%s", meta.get("as_of"))

        # 通知前端: 生成完成且已归档(archived=true 让前端只刷新列表, 不重复归档)
        if quote_service:
            quote_service.push_review_event(json.dumps(
                {"type": "done", "archived": True}, ensure_ascii=False))

        # 推送到飞书(可选): 运行时读取配置, 用户改设置下次触发即生效。
        # 失败静默降级, 不影响已归档的报告。
        _maybe_push_review(content, meta)
    except Exception as e:
        logger.exception("scheduled review failed: %s", e)
        # 兜底: 异常时通知前端停止「生成中」状态, 避免页面卡在 streaming
        try:
            app_state = _get_app_state()
            qs = getattr(app_state, "quote_service", None) if app_state else None
            if qs:
                import json as _json
                qs.push_review_event(_json.dumps(
                    {"type": "error", "message": "复盘生成异常,请稍后手动重试"},
                    ensure_ascii=False))
        except Exception:
            pass


async def _stream_review_with_retry(repo, quote_service, depth_service) -> tuple[str, dict]:
    """流式生成复盘, 每个事件推 SSE + 累积内容。LLM 断流时最多重试 2 次。

    返回 (content, meta)。重试时推一个 retry 事件让前端清空已累积内容重新开始。
    成功(收到 done/无 error)或耗尽重试后返回。
    """
    import asyncio
    import json

    from app.services.market_recap import recap_market_stream

    max_attempts = 3  # 初次 + 2 次重试
    last_meta: dict = {}
    content_parts: list[str] = []

    for attempt in range(1, max_attempts + 1):
        content_parts = []  # 每次重试重新累积
        failed = False
        try:
            async for evt_json in recap_market_stream(repo, quote_service, depth_service):
                evt = json.loads(evt_json)
                t = evt.get("type")

                # 推给前端(让开着页面的用户实时看到, 与手动一致)
                if quote_service:
                    quote_service.push_review_event(evt_json)

                if t == "meta":
                    last_meta = evt
                elif t == "delta" and evt.get("content"):
                    content_parts.append(evt["content"])
                elif t == "error":
                    failed = True
                    logger.warning("scheduled review stream error (attempt %d/%d): %s",
                                   attempt, max_attempts, evt.get("message"))
                    break  # 触发重试
                elif t == "done":
                    # 正常完成
                    return "".join(content_parts), last_meta
            # 流自然结束(无 done 事件)且有内容, 视为成功
            if content_parts and not failed:
                return "".join(content_parts), last_meta
        except Exception as e:
            # LLM 断流等异常(httpx.RemoteProtocolError)落到这里
            failed = True
            logger.warning("scheduled review stream exception (attempt %d/%d): %s",
                           attempt, max_attempts, e)

        # 失败: 决定是否重试
        if attempt < max_attempts:
            logger.info("scheduled review retrying in 3s (attempt %d → %d)", attempt, attempt + 1)
            # 通知前端: 即将重试, 清空已累积内容重新开始
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "retry", "attempt": attempt + 1}, ensure_ascii=False))
            await asyncio.sleep(3)

    # 耗尽重试, 返回已累积内容(可能为空)和最后 meta
    return "".join(content_parts), last_meta


def _maybe_push_review(content: str, meta: dict) -> None:
    """复盘报告归档后, 按 review_push_channels 选定的外部工具逐个推送完整报告。

    定时生成与手动生成共用本函数 (手动归档端点 POST /api/market-recap/reports 也会调用)。
    channels 为空则不推送; 'feishu' 复用监控中心的全局飞书 Webhook 通道。
    推送失败静默降级 (Webhook 是辅助通道), 不影响已归档的报告。
    """
    try:
        from app.services import preferences, webhook_adapter

        channels = preferences.get_review_push_channels()
        if not channels:
            return

        emotion = f"{meta.get('emotion_label') or ''}".strip()
        as_of = meta.get("as_of") or ""
        subtitle = as_of + (f" · 情绪 {emotion}" if emotion else "")

        for ch in channels:
            if ch == "feishu":
                url = preferences.get_feishu_webhook_url()
                if not url:
                    logger.info("review push(feishu) skipped: webhook not configured")
                    continue
                secret = preferences.get_feishu_webhook_secret()
                ok = webhook_adapter.send_feishu_card(
                    url, "每日复盘", subtitle, content, secret
                )
                logger.info("review push(feishu) %s", "sent" if ok else "failed")
            elif ch == "wecom":
                url = preferences.get_wecom_webhook_url()
                if not url:
                    logger.info("review push(wecom) skipped: webhook not configured")
                    continue
                # 企业微信 markdown 标题已含一级标题, subtitle 拼到正文首行
                full_body = (f"**{subtitle}**\n\n{content}" if subtitle else content)
                ok = webhook_adapter.send_wecom_markdown(
                    url, "每日复盘", full_body
                )
                logger.info("review push(wecom) %s", "sent" if ok else "failed")
            # 未来更多渠道在此追加分支
    except Exception as e:
        logger.warning("review push error: %s", e)


def _register_review_job(scheduler, repo, hour: int, minute: int) -> None:
    """注册/更新定时复盘 job(工作日 mon-fri, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(改时间时) 共用。
    用 replace_existing=True, 重复注册只更新 trigger。

    注意: _run_scheduled_review 是协程函数, 必须把函数对象本身(配合 args)传给
    add_job, 而非用 lambda 包裹 —— 否则 APScheduler 会把 lambda 当同步函数在线程池
    执行, 仅得到一个未 await 的协程对象, 复盘实际不会运行。
    """
    scheduler.add_job(
        _run_scheduled_review,
        args=[repo],
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=hour, minute=minute,
                            timezone="Asia/Shanghai"),
        id=REVIEW_JOB_ID,
        misfire_grace_time=7200,  # 复盘非关键, 允许 2 小时内补跑
        replace_existing=True,
    )


def _run_market_daily_scheduled(
    repo: KlineRepository, capset: CapabilitySet, market: str, *, full_history: bool = False,
) -> dict:
    """严格刷新 HK/US universe 后执行日 K；失败不回退到 demo 池。

    full_history=False (默认, 服务内调度口径): incremental + 近 365 天窗口,
        只推进增量, 不触碰 legacy 旧分区。
    full_history=True (legacy 修复口径): mode="full" + 1998-06-01 起全窗口,
        走 merge 闸门四道护栏 (见 market_daily.py) 整体替换旧分区并留
        _repair_backup 审计。run_hk_daily_catchup.py --full-history 走此路。
    """
    from app.services import hk_data_adapter, market_daily_sync
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        try_acquire_run_slot,
    )

    market = market.upper()
    job_id, is_new = job_store.create(long_running=True)
    if not is_new:
        logger.info("scheduled %s daily skipped: active job=%s", market, job_id)
        return {"status": "reused", "job_id": job_id, "market": market}
    if not try_acquire_run_slot(job_id):
        job_store.fail(job_id, f"scheduled {market} daily skipped: data task occupied")
        return {"status": "skipped", "job_id": job_id, "market": market}

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None, skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    try:
        job_store.start(job_id)
        progress("sync_instruments", 2, f"同步 {market} 全量标的池…")
        if market == "HK":
            rows = hk_data_adapter.sync_hk_instruments(repo.store.data_dir, allow_demo=False)
        else:
            rows = hk_data_adapter.sync_us_instruments(repo.store.data_dir, allow_demo=False)
        progress("sync_instruments", 10, f"{market} 标的池已更新，共 {rows} 只")
        if full_history:
            sync_start = date(1998, 6, 1)
            sync_mode = "full"
        else:
            sync_start = date.today() - timedelta(days=365)
            sync_mode = "incremental"
        result = market_daily_sync.run_market_daily_sync(
            repo=repo,
            capset=capset,
            job_id=job_id,
            market=market,
            symbols=None,
            start_date=datetime.combine(sync_start, time.min),
            end_date=datetime.combine(date.today(), time.max),
            mode=sync_mode,
            on_progress=progress,
        )
        result["universe_sync_rows"] = rows
        result["market_timezone"] = "Asia/Hong_Kong" if market == "HK" else "America/New_York"
        job_store.succeed(job_id, result)
        return result
    except JobCancelledError:
        raise
    except Exception as exc:
        logger.exception("scheduled %s daily failed", market)
        job_store.fail(job_id, str(exc))
        return {"status": "failed", "job_id": job_id, "market": market, "error": str(exc)}
    finally:
        release_run_slot(job_id)


def _partition_date_norm(value: object) -> date | None:
    """分区 max(date) 归一化: datetime 是 date 的子类, 必须先判 datetime 再判 date。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _partition_latest_distribution(
    base: Path, market: str, sample: int = 30
) -> Counter[date]:
    """扫 {base}/symbol=*.<HK|US>/part.parquet, 统计各分区 max(date) 的分布。

    返回 {日期: 该日期作为分区最新日的分区数}; 空 Counter 表示目录不存在或
    全部读取失败 (如全新部署)。逐文件只读 footer 级 max(date), 成本可控。
    """
    suffix = ".HK" if market.upper() == "HK" else ".US"
    if not base.exists():
        return Counter()
    parts = sorted(base.glob(f"symbol=*{suffix}/part.parquet"))
    if not parts:
        return Counter()
    step = max(1, len(parts) // sample)
    picked = parts[::step][:sample]

    counts: Counter[date] = Counter()
    for path in picked:
        try:
            latest = pl.scan_parquet(path).select(pl.col("date").max()).collect().item()
            normalized = _partition_date_norm(latest)
            if normalized is not None:
                counts[normalized] += 1
        except Exception:
            continue
    return counts


def _h6_latest_distribution(
    data_dir: Path, market: str, sample: int = 30
) -> Counter[date]:
    """抽样港美 H6 (kline_daily/symbol=*.HK|.US) 分区, 统计各分区 max(date) 的分布。"""
    return _partition_latest_distribution(data_dir / "kline_daily", market, sample=sample)


def _enriched_latest_distribution(
    data_dir: Path, market: str, sample: int = 30
) -> Counter[date]:
    """抽样港美 enriched (kline_hk_us_enriched/symbol=*.HK|.US) 的 max(date) 分布。

    与 H6 判据互补: 两侧覆盖会不一致 —— 09-18 实测港股 H6 最新停在 09-16
    (压根没有 09-17), 而 enriched 侧有 72/2812 只被零散标的带到 09-17。
    只看 H6 会漏判这种"enriched 被推到更新的残缺日"的情况。
    """
    return _partition_latest_distribution(
        data_dir / "kline_hk_us_enriched", market, sample=sample
    )


def _h6_latest_by_sampling(data_dir: Path, market: str, sample: int = 30) -> date | None:
    """抽样港美 H6 分区取 max(date) 的众数, 作为该市场新鲜度的轻量判定。

    返回 None 表示无分区或全部读取失败 (如全新部署), 不触发 catch-up。
    """
    counts = _h6_latest_distribution(data_dir, market, sample=sample)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def _market_partial_sync_pending(
    data_dir: Path, market: str, threshold: float = 0.5
) -> bool:
    """最新交易日只同步了一部分标的 → 补跑仍有必要。

    场景: 盘后调度窗口 (HK 18:30 / US 08:30) 服务不在线, 当日全量同步没跑,
    只有自选股/零散标的被增量带到最新日。此时众数最新日仍是前一交易日,
    单纯按"日期落后几天"判定会漏判, 该市场就此停在少数标的撑起来的日期上。

    判据: H6 与 enriched 两侧任一满足「最新日期的分区数 < 众数日期分区数 *
    threshold」即视为部分同步 (典型 09-17 港股 72/2812 只 ≈ 2.6%)。
    两侧都看是因为它们的覆盖会不一致, 只看一侧会漏判。
    """
    for counts in (
        _h6_latest_distribution(data_dir, market),
        _enriched_latest_distribution(data_dir, market),
    ):
        if not counts:
            continue
        modal_date, modal_n = counts.most_common(1)[0]
        newest = max(counts)
        if newest <= modal_date:
            continue  # 该侧最新日就是完成度最高的那天, 同步完整
        if counts[newest] < modal_n * threshold:
            return True
    return False


# 各市场调度窗口 (start_scheduler 注册时刻) + 30 分钟缓冲; 早于此时刻启动
# 视为"窗口未过", catch-up 不判落后 (当日调度本身可能正常触发)。
_MARKET_DAILY_CATCHUP_AFTER = {"HK": (18, 30), "US": (8, 30)}

# 数据落后容差 (自然日): 覆盖周末与常规节假日, 避免 catch-up 依赖交易日历。
# 超长假期 (如春节) 会多触发一次 incremental 空转, 框架幂等无害。
_MARKET_DAILY_STALENESS_DAYS = {"HK": 4, "US": 3}


def _market_daily_catchup_needed(data_dir: Path, market: str, now: datetime) -> bool:
    """判定某市场是否需要启动补跑: 调度窗口已过 + (日期落后超容差 或 部分同步)。"""
    hour, minute = _MARKET_DAILY_CATCHUP_AFTER.get(market, (99, 0))
    if (now.hour, now.minute) < (hour, minute):
        return False
    latest = _h6_latest_by_sampling(data_dir, market)
    if latest is None:
        return False
    staleness = (now.date() - latest).days
    if staleness > _MARKET_DAILY_STALENESS_DAYS[market]:
        return True
    # 日期不落后 ≠ 同步完整: 最新日可能只有零散标的到位 (服务错开调度窗口时
    # 的典型残留)。此时同样要补跑, 否则该市场长期停在"少数标的撑起来的日期"。
    return _market_partial_sync_pending(data_dir, market)


def _provider_cooling_down(market: str) -> bool:
    """该市场依赖的数据源是否处于限流冷却期。

    冷却期内启动补跑 = 全量标的逐个本地快速失败, 0 数据收益 (09-18 实测:
    美股 6071 只全 provider_error, job 9 秒空转收场)。此时延后比硬跑合理。
    查询失败按"未冷却"处理, 不影响原有补跑行为。
    """
    if market.upper() != "US":
        return False  # 港股的腾讯/新浪源另有熔断, 暂不纳入
    try:
        from app.data_providers.yfinance_provider import yf_circuit_blocked

        return yf_circuit_blocked()
    except Exception:
        return False


def run_market_daily_catchup(
    repo: KlineRepository, capset: CapabilitySet, *, now: datetime | None = None
) -> dict:
    """服务启动后的兜底补跑: 港美 H6 因调度窗口(18:00/08:00)与服务在线时段
    错配而停更时, 启动时检测并补齐。

    仅在"窗口已过 + 数据落后"时触发, 复用 _run_market_daily_scheduled 的
    严格 universe 刷新 + incremental 同步 (job_store 占坑防止与正常调度并发)。
    供 main.py 的 daemon Timer 延迟调用, 任何失败静默 (不影响启动)。
    now 仅测试注入用。
    """
    from zoneinfo import ZoneInfo

    results: dict = {}
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    for market in ("HK", "US"):
        try:
            if not _market_daily_catchup_needed(repo.store.data_dir, market, now):
                continue
            if _provider_cooling_down(market):
                logger.info(
                    "market_daily catchup: %s 数据源冷却中, 延后补跑 (此刻跑必然空转)",
                    market,
                )
                results[market] = {"status": "deferred", "market": market,
                                   "reason": "provider_cooling_down"}
                continue
            logger.info("market_daily catchup: %s H6 数据落后, 启动补跑", market)
            results[market] = _run_market_daily_scheduled(repo, capset, market)
        except Exception:
            logger.exception("market_daily catchup failed for %s", market)
    if not results:
        logger.info("market_daily catchup: 港美 H6 均为最新, 无需补跑")
    return results


# A 股盘后管道(默认 15:30)自身约需十余分钟, 窗口判定给 30 分钟缓冲;
# 正常日有当日数据会被落后容差挡掉, 不会重复跑。
# 落后容差 3 个自然日, 覆盖周末(周五→周一 3 天), 与港美 catch-up 同口径。
_CN_PIPELINE_CATCHUP_AFTER_MINUTES = 30
_CN_STALENESS_DAYS = 3


def _cn_latest_daily_date(data_dir: Path) -> date | None:
    """A 股日 K 最新交易日 (date= 分区目录名)。"""
    latest: date | None = None
    for directory in (data_dir / "kline_daily").glob("date=*"):
        try:
            value = date.fromisoformat(directory.name.removeprefix("date="))
        except ValueError:
            continue
        latest = value if latest is None or value > latest else latest
    return latest


def _cn_catchup_needed(data_dir: Path, now: datetime) -> bool:
    """判定 A 股是否需要启动兜底补跑: 工作日 + 管道窗口已过 + 日 K 落后超容差。"""
    from app.services import preferences

    if now.weekday() >= 5:  # 周末不补, 等下个工作日正常调度
        return False
    schedule = preferences.get_pipeline_schedule()
    window = schedule["hour"] * 60 + schedule["minute"] + _CN_PIPELINE_CATCHUP_AFTER_MINUTES
    if (now.hour * 60 + now.minute) < window:
        return False
    latest = _cn_latest_daily_date(data_dir)
    if latest is None:  # 完全无数据: 管道本身会建基线, 交给它跑
        return True
    return (now.date() - latest).days > _CN_STALENESS_DAYS


def run_daily_pipeline_catchup(
    repo: KlineRepository, capset: CapabilitySet, *, now: datetime | None = None
) -> dict:
    """服务启动后的 A 股盘后管道兜底补跑。

    港美已有 market_daily catch-up, A 股此前没有: 服务在 15:30 之后才启动
    (或整天没开) 时, 当日的日 K + enriched 会一直缺到次日调度。这里在启动时
    检测"窗口已过 + 日 K 落后"并补跑一次, 复用 _scheduled_pipeline_task
    (JobStore 占坑防并发 + 成功后跑周度挖掘), 与正常调度完全同构。
    now 仅测试注入用; 任何失败静默, 不影响启动。
    """
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    try:
        if not _cn_catchup_needed(repo.store.data_dir, now):
            logger.info("daily_pipeline catchup: A 股日 K 无需补跑")
            return {"status": "skipped"}
        logger.info("daily_pipeline catchup: A 股日 K 落后, 启动补跑")
        _scheduled_pipeline_task(lambda: run_pipeline_then_refresh(repo, capset))
        return {"status": "ok"}
    except Exception:
        logger.exception("daily_pipeline catchup failed")
        return {"status": "failed"}


def start_scheduler(repo: KlineRepository, capset: CapabilitySet) -> AsyncIOScheduler:
    """启动调度器。

    工作日 09:10 — 同步个股维表
    工作日 HH:MM — 盘后管道（时间由用户偏好决定，默认 15:30）
    """
    from app.services import preferences
    sched = preferences.get_pipeline_schedule()
    inst_sched = preferences.get_instruments_schedule()

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 盘前: 同步 instruments（时间由偏好决定）
    def _instruments_task(on_progress=None):
        emit = on_progress or _noop
        emit("sync_instruments", 0, "同步个股维表…")
        result = run_instruments_sync(repo)
        emit("done", 100, f"个股维表同步完成,{result.get('instruments_rows', 0)} 只标的")
        return result

    scheduler.add_job(
        lambda: _run_tracked(_instruments_task, "instruments_sync"),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=inst_sched["hour"], minute=inst_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="pre_market_instruments",
        misfire_grace_time=1800,
        replace_existing=True,
    )

    # 港美股独立日 K 闭环：先严格刷新真实 instruments，再冻结 universe 同步。
    # 时间按北京时间安排在各市场通常收盘之后；失败任务保留在 JobStore，
    # 不影响 A 股盘后管道，也不会回退成 demo universe。
    def _scheduled_market_daily(market: str):
        app_state = _get_app_state()
        capset_live = getattr(app_state, "capabilities", None) or capset
        return _run_market_daily_scheduled(repo, capset_live, market)

    scheduler.add_job(
        lambda: _scheduled_market_daily("HK"),
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=0,
                            timezone="Asia/Shanghai"),
        id="market_daily_hk",
        misfire_grace_time=7200,
        replace_existing=True,
    )
    scheduler.add_job(
        lambda: _scheduled_market_daily("US"),
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=0,
                            timezone="Asia/Shanghai"),
        id="market_daily_us",
        misfire_grace_time=7200,
        replace_existing=True,
    )

    # 盘后: 日 K + enriched（时间由偏好决定）
    def _pipeline_then_refresh(on_progress=None):
        return run_pipeline_then_refresh(repo, capset, on_progress=on_progress)

    scheduler.add_job(
        lambda: _scheduled_pipeline_task(_pipeline_then_refresh),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=sched["hour"], minute=sched["minute"],
                            timezone="Asia/Shanghai"),
        id="daily_pipeline",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 盘后: 五档盘口 sealed 定版(时间由偏好决定, 默认15:02, 范围15:01~18:00)
    depth_sched = preferences.get_depth_finalize_time()

    def _depth_finalize():
        depth_svc = getattr(_get_app_state(), "depth_service", None) if _get_app_state() else None
        if depth_svc:
            depth_svc.finalize()

    scheduler.add_job(
        _depth_finalize,
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=depth_sched["hour"], minute=depth_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="depth_finalize",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 周期性能力重探: 付费 Key 中途过期/续费无需重启即可被发现。
    # 只热更新 app.state.capabilities(API 端点、盘后管道 _pipeline_then_refresh 均读它);
    # 档位变化记 WARNING, 让「Key 失效」在日志/前端可见, 不再静默按旧档位打 403 端点。
    def _reprobe_capabilities():
        from app.tickflow.policy import detect_capabilities, tier_label
        app_state = _get_app_state()
        if app_state is None:
            return
        try:
            old = getattr(app_state, "capabilities", None)
            old_n = len(old.all()) if old else -1
            new_capset = detect_capabilities(force=True)
            app_state.capabilities = new_capset
            new_n = len(new_capset.all())
            if old_n != new_n:
                logger.warning(
                    "能力集变化: %d → %d capabilities (档位=%s)。Key 过期/续费或端点波动, "
                    "已热更新 app.state.capabilities。", old_n, new_n, tier_label(),
                )
        except Exception as e:
            logger.warning("周期能力重探失败(保留现有能力集): %s", e)

    scheduler.add_job(
        _reprobe_capabilities,
        trigger=IntervalTrigger(minutes=60),
        id="reprobe_capabilities",
        misfire_grace_time=600,
        replace_existing=True,
    )

    # 定时复盘 (AI 大盘复盘报告): 工作日到点自动生成并归档。
    # 默认关闭 —— 仅当用户在复盘页开启时才注册 job。
    # 复用 recap_market_once(非流式) + market_recap_reports.save_report(落盘)。
    # quote_service / depth_service 通过 _get_app_state() 延迟取用。
    review_sched = preferences.get_review_schedule()
    if review_sched["enabled"]:
        _register_review_job(scheduler, repo, review_sched["hour"], review_sched["minute"])
        logger.info("scheduled_review enabled @%02d:%02d mon-fri",
                    review_sched["hour"], review_sched["minute"])

    # 热点工作区同步 (A 股东财概念/行业板块): 工作日盘中每 30 分钟。
    # 港美无 topic 数据源, job 内部走 fail-closed 不影响本调度器。
    try:
        from app.jobs.hotspot_sync import register_hotspot_jobs

        register_hotspot_jobs(scheduler, repo.store.data_dir)
    except Exception as e:
        logger.warning("hotspot sync job registration failed (soft): %s", e)

    scheduler.start()
    logger.info("scheduler started; instruments@%02d:%02d, pipeline@%02d:%02d, depth@%02d:%02d mon-fri",
                inst_sched["hour"], inst_sched["minute"], sched["hour"], sched["minute"],
                depth_sched["hour"], depth_sched["minute"])
    return scheduler


# app_state 延迟引用(start_scheduler 在 lifespan 早期调用, app.state 可能还没就绪)
_app_state_ref = None


def set_app_state(app_state) -> None:
    """lifespan 注册 app.state 引用, 供 scheduled job 访问 depth_service 等单例。"""
    global _app_state_ref
    _app_state_ref = app_state


def _get_app_state():
    return _app_state_ref
