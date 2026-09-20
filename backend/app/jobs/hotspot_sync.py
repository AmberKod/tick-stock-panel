"""热点工作区同步 job (A 股 / 港股 / 美股三个市场)。

- 定时: 交易日盘中每 30 分钟一次 (:05 / :35, Asia/Shanghai)。
  - cn: 工作日 09:05 ~ 15:35  (A 股 09:30-15:00, 15:05/15:35 是收盘后定版)
  - hk: 工作日 09:05 ~ 15:35  (港股 09:30-16:00 HKT, 与北京时间同时区)
  - us: 拆成**两个** job, 因为美股夏令时是 21:30 - 次日 04:00 北京时间 (跨日),
    而 ``day_of_week`` 是**日历日**口径:
      * ``hotspot_sync_us``      21:05 ~ 23:35, mon-fri  (夜盘前半, 当天)
      * ``hotspot_sync_us_late`` 00:05 ~ 03:35, tue-sat  (夜盘后半, 已是次日凌晨)
    ``0-3`` 段配 ``tue-sat`` 才覆盖得到周一~周五夜盘的后半段 (含周五夜盘 →
    周六凌晨); 若整段用 ``mon-fri``, 周五夜盘会断档、而周一凌晨 (美东周日,
    美股休市) 反而空转。
  :05 那一轮在开盘前会落到上一交易日快照并标 stale, 与 A 股现有行为一致。
- 手动: ``python -m app.jobs.hotspot_sync --once --market cn|hk|us`` 立即拉一次。
- 数据源: 三个市场都是本地聚合 (A 股 = 同花顺概念 x 行情, 港美 = instruments
  行业 x 行情), 都不依赖外部板块接口。akshare 东财源仍在代码里, 但**不再作为
  默认源** (见 run_hotspot_sync), 需要时显式注入 source override。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

HOTSPOT_SYNC_JOB_ID = "hotspot_sync_cn"
HOTSPOT_SYNC_JOB_ID_HK = "hotspot_sync_hk"
HOTSPOT_SYNC_JOB_ID_US = "hotspot_sync_us"
HOTSPOT_SYNC_JOB_ID_US_LATE = "hotspot_sync_us_late"

# 三个市场都是"交易日盘中每 30 分钟": 港美热点与 A 股一样是本地聚合
# (instruments 行业 x 行情), 成本主要是扫一次 parquet + 一次批量实时,
# 没有外部接口配额/限流问题, 一次运行秒级完成, 所以沿用 A 股的 30 分钟口径。
_SYNC_MINUTE = "5,35"
_SYNC_TIMEZONE = "Asia/Shanghai"

# (job_id, market, hour, day_of_week) —— 美股占两行: 跨日交易时段在日历上属于
# 两天, day_of_week 只能按日历日判, 所以 21-23 段与 0-3 段必须各自配对。
#   21-23 + mon-fri → 周一~周五夜盘前半 (21:05 ~ 23:35)
#   0-3   + tue-sat → 周一~周五夜盘后半 (次日 00:05 ~ 03:35, 含周五夜盘)
_SYNC_JOBS: tuple[tuple[str, str, str, str], ...] = (
    (HOTSPOT_SYNC_JOB_ID, "cn", "9-15", "mon-fri"),   # 09:30-15:00 北京时间
    (HOTSPOT_SYNC_JOB_ID_HK, "hk", "9-15", "mon-fri"),  # 09:30-16:00 HKT (与北京时间同时区)
    (HOTSPOT_SYNC_JOB_ID_US, "us", "21-23", "mon-fri"),
    (HOTSPOT_SYNC_JOB_ID_US_LATE, "us", "0-3", "tue-sat"),
)


def run_hotspot_sync(
    data_dir: Any,
    *,
    market: str = "cn",
    source: Any = None,
) -> dict:
    """执行一次热点同步 (供调度器与 CLI 共用)。

    ``source`` 为 None 时交给 ``select_source`` 的默认值 —— 与生产 API 同一条路径
    (A 股 = 本地同花顺概念 x 行情, 港美 = instruments 行业 x 行情), 依然是
    fail-closed, 不回退 stub。

    2026-09-20 起不再默认构造 ``AkshareHotspotSource``: 它依赖 push2.eastmoney.com,
    在代理环境不可达 (实测 28.7s 超时后返回空列表), 工作日每半小时用死源刷一次
    会把 job_state.last_status 刷成 empty、热点页显示"无数据"。akshare 源保留在
    代码里, 需要时显式 ``run_hotspot_sync(..., source=AkshareHotspotSource(...))``
    注入 override。
    """
    from app.services.hotspot.service import refresh_hotspots

    result = refresh_hotspots(data_dir, market=market, source=source)
    status = result.get("status")
    if status in {"ok", "degraded"}:
        logger.info(
            "hotspot sync [%s] %s: rows=%s provider=%s",
            market, status, result.get("rows"), result.get("provider"),
        )
    else:
        logger.warning("hotspot sync [%s] %s", market, result)
    return result


def _add_market_job(
    scheduler: AsyncIOScheduler,
    data_dir: Any,
    *,
    job_id: str,
    market: str,
    hour: str,
    day_of_week: str,
) -> None:
    """注册单个市场的热点同步 job (交易日盘中每 30 分钟, misfire 宽限 10 分钟)。

    ``market`` 通过默认参数绑定进 lambda (而不是外部循环变量), 避免晚绑定
    把所有 job 都指向最后一个市场。
    """
    scheduler.add_job(
        lambda market=market: run_hotspot_sync(data_dir, market=market),
        trigger=CronTrigger(
            day_of_week=day_of_week,
            hour=hour,
            minute=_SYNC_MINUTE,
            timezone=_SYNC_TIMEZONE,
        ),
        id=job_id,
        misfire_grace_time=600,
        replace_existing=True,
    )


def register_hotspot_jobs(scheduler: AsyncIOScheduler, data_dir: Any) -> None:
    """把三个市场的热点同步挂上调度器 (交易日盘中每 30 分钟)。

    注册顺序固定为 cn / hk / us / us_late (cn 保持 index 0, 兼容既有测试与运维约定)。
    """
    for job_id, market, hour, day_of_week in _SYNC_JOBS:
        _add_market_job(
            scheduler, data_dir, job_id=job_id, market=market, hour=hour, day_of_week=day_of_week,
        )
    logger.info(
        "hotspot sync registered: cn/hk @ mon-fri 09:05-15:35, us @ mon-fri 21:05-23:35"
        " + tue-sat 00:05-03:35, every 30min (Asia/Shanghai)",
    )


# ----------------------------------------------------------------------
# CLI: python -m app.jobs.hotspot_sync --once [--market cn] [--data-dir PATH]
# ----------------------------------------------------------------------

def _cli() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="手动执行一次热点同步")
    parser.add_argument("--once", action="store_true", help="立即同步一次后退出")
    parser.add_argument("--market", default="cn", choices=("cn", "hk", "us"))
    parser.add_argument("--data-dir", default=None, help="数据根目录 (默认 settings.data_dir)")
    args = parser.parse_args()

    if not args.once:
        parser.print_help()
        return 2

    if args.data_dir:
        data_dir = Path(args.data_dir)
    else:
        from app.config import settings

        data_dir = Path(settings.data_dir)

    result = run_hotspot_sync(data_dir, market=args.market)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") in {"ok", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
