"""热点工作区同步 job (A 股东财概念/行业板块)。

- 定时: 工作日盘中 09:05 ~ 15:35 每 30 分钟一次 (东财板块快照),
  覆盖开盘 / 午盘 / 收盘定版。
- 手动: ``python -m app.jobs.hotspot_sync --once`` 立即拉一次。
- 港美市场无 topic 数据源, refresh 走 service 层 fail-closed (skipped)。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

HOTSPOT_SYNC_JOB_ID = "hotspot_sync_cn"


def run_hotspot_sync(
    data_dir: Any,
    *,
    market: str = "cn",
    source: Any = None,
) -> dict:
    """执行一次热点同步 (供调度器与 CLI 共用)。

    A 股默认显式使用 akshare 东财源 —— 不依赖 select_source 的默认值,
    保证 CLI/调度路径与生产 API 行为一致 (fail-closed, 不回退 stub)。
    """
    from app.services.hotspot.service import refresh_hotspots

    if source is None and market == "cn":
        from app.services.hotspot.akshare_source import AkshareHotspotSource

        source = AkshareHotspotSource()
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


def register_hotspot_jobs(scheduler: AsyncIOScheduler, data_dir: Any) -> None:
    """把热点同步挂上调度器 (工作日盘中每 30 分钟)。"""
    scheduler.add_job(
        lambda: run_hotspot_sync(data_dir),
        trigger=CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="5,35",
            timezone="Asia/Shanghai",
        ),
        id=HOTSPOT_SYNC_JOB_ID,
        misfire_grace_time=600,
        replace_existing=True,
    )
    logger.info("hotspot sync registered @ mon-fri 09:05-15:35 every 30min")


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
