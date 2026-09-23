"""港美异动监控 job (日线收盘口径, 挂在各市场日线同步之后)。

为什么**不跟着 A 股轮询走** (2026-09-22 数据地基批 #6 的判据被证伪后修订):

1. ``build_hk_us_abnormal_overview(data_dir, market, ...)`` 只收 data_dir,
   **不收 quote_service** —— 它只读 ``kline_hk_us_enriched`` parquet, 不消费
   轮询拉回的实时行情 (对比 A 股 ``abnormal_moves.build_overview(repo, self)``
   传了 quote_service 做实时叠加)。
2. 实时 universe 只有 CN_Equity_A / CN_ETF / CN_Index, **没有港美实时行情**。
3. 港美 parquet **一天只写一次**: 日线同步 HK cron mon-fri 18:00、US cron
   mon-fri 08:00 (Asia/Shanghai)。

⇒ 港美异动**本质就是日线收盘口径**: 美股盘中 parquet 最新日期恒为前一交易日,
把 A 股轮询窗口扩到 21:30-04:00 只会整夜拉 A 股 5000-7000 只、收益为零。所以
本 job 在各自日线同步的 30 分钟缓冲之后独立触发, 不碰 ``_market_phase``。

触发时刻: **HK 18:35 / US 08:35** (Asia/Shanghai) —— 对齐现成常量
``daily_pipeline._MARKET_DAILY_CATCHUP_AFTER = {"HK": (18,30), "US": (8,30)}``
(已含 30 分钟缓冲) 再 +5 分钟, 与日线同步同一 mon-fri 日历。

真正的门控 (快照新鲜度 + 是否已收盘) 在 ``quote_service._hk_us_monitor_gate``,
本模块只负责"到点调用 + 软失败记录"。
"""
from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

HK_US_MONITOR_JOB_ID_HK = "hk_us_monitor_hk"
HK_US_MONITOR_JOB_ID_US = "hk_us_monitor_us"

# (job_id, market, hour, minute, day_of_week) —— 与港美日线同步同步日历 (mon-fri),
# 时刻取"同步窗口 + 35 分钟": 18:35 / 08:35 Asia/Shanghai。
_MONITOR_JOBS: tuple[tuple[str, str, int, int, str], ...] = (
    (HK_US_MONITOR_JOB_ID_HK, "HK", 18, 35, "mon-fri"),
    (HK_US_MONITOR_JOB_ID_US, "US", 8, 35, "mon-fri"),
)
_MONITOR_TIMEZONE = "Asia/Shanghai"


def _quote_service() -> Any | None:
    """延迟取 quote_service 单例 (start_scheduler 早于 app.state 就绪)。"""
    from app.jobs.daily_pipeline import _get_app_state

    app_state = _get_app_state()
    return getattr(app_state, "quote_service", None) if app_state else None


def run_hk_us_monitor(market: str = "HK", *, quote_service: Any = None) -> dict:
    """到点执行一次该市场的异动监控评估 (供调度器与 CLI 共用)。

    任何失败都在本函数内收敛为 ``{"status": "failed", ...}`` —— 监控是旁路,
    job 失败不得影响调度器与主流程。
    """
    market = str(market or "").strip().upper()
    service = quote_service if quote_service is not None else _quote_service()
    if service is None:
        return {"market": market, "status": "skipped", "events": 0, "alerts": 0,
                "reason": "quote_service 未就绪"}
    try:
        result = service.evaluate_hk_us_monitors(market)
    except Exception as exc:
        logger.warning("港美异动监控 job 失败 (%s): %s", market, exc)
        return {"market": market, "status": "failed", "events": 0, "alerts": 0,
                "reason": str(exc)}
    status = result.get("status")
    if status == "ok":
        logger.info("港美异动监控 [%s] ok: events=%s alerts=%s",
                    market, result.get("events"), result.get("alerts"))
    else:
        logger.info("港美异动监控 [%s] %s: %s", market, status, result.get("reason"))
    return result


def register_hk_us_monitor_jobs(scheduler: AsyncIOScheduler, *, quote_service: Any = None) -> None:
    """把港美异动监控挂上调度器 (HK 18:35 / US 08:35, mon-fri Asia/Shanghai)。

    ``market`` 通过默认参数绑定进 lambda (而非循环变量), 避免晚绑定把所有 job
    都指向最后一个市场。
    """
    for job_id, market, hour, minute, day_of_week in _MONITOR_JOBS:
        scheduler.add_job(
            lambda market=market, service=quote_service: run_hk_us_monitor(market, quote_service=service),
            trigger=CronTrigger(
                day_of_week=day_of_week,
                hour=hour,
                minute=minute,
                timezone=_MONITOR_TIMEZONE,
            ),
            id=job_id,
            misfire_grace_time=600,
            replace_existing=True,
        )
    logger.info(
        "hk_us monitor registered: HK @ 18:35, US @ 08:35 mon-fri (Asia/Shanghai)",
    )
