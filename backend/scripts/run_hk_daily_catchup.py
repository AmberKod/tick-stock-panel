#!/usr/bin/env python3
"""港股日 K 补跑驱动 (脱离 FastAPI 服务独立运行)。

背景:
    服务内 catch-up 依赖"调度窗口已过"判定, 白天启动服务不会触发 HK 补跑;
    本脚本直接调用 _run_market_daily_scheduled (严格 universe 刷新 +
    job_store 占坑), 与服务内 18:00 调度完全同构。

    默认 (无 --full-history): incremental + 近 365 天窗口, 只推进增量,
    不触碰 legacy 旧 date 分区 —— 即服务内调度的口径。

    --full-history: mode="full" + 1998-06-01 起全窗口, 走 merge 闸门
    四道护栏 (datetime 归一化 / 15% 有界丢失 / 已确认交易日基准 /
    未收盘当日行剔除) 整体替换 legacy 分区并留 _repair_backup 审计。
    09-16 40 只抽样 29/40 (00xxx 最差段); 全量约 4~5 小时。

用法:
    python backend/scripts/run_hk_daily_catchup.py          # 交互确认后运行 (增量)
    python backend/scripts/run_hk_daily_catchup.py --yes    # 跳过确认 (自动化, 增量)
    python backend/scripts/run_hk_daily_catchup.py --market US --yes
    python backend/scripts/run_hk_daily_catchup.py --full-history --yes  # legacy 全窗口修复

注意:
    增量补跑约 1-1.5 小时 (2811 只逐只拉取); --full-history 约 4~5 小时。
    脚本无 --help 之外的参数时必须显式确认 —— 09-15 曾因 `--help` 被忽略
    直接开跑造成误启动。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_hk_daily_catchup")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="港股/美股日 K 补跑驱动 (与服务内调度同构, 免服务免窗口判定)",
    )
    parser.add_argument(
        "--market", choices=["HK", "US"], default="HK",
        help="补跑市场 (默认 HK)",
    )
    parser.add_argument(
        "--full-history", action="store_true",
        help="全窗口 legacy 修复 (mode=full + 1998-06-01 起, 约 4~5 小时); "
             "默认仅增量推进近 365 天",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="跳过交互确认 (自动化场景); 默认需输入 y 确认",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = _parse_args()
    mode_desc = "全窗口 legacy 修复 (mode=full, 约 4~5 小时)" if args.full_history else "增量补跑 (近 365 天, 约 1-1.5 小时)"
    if not args.yes:
        answer = input(
            f"即将对 {args.market} 发起{mode_desc}, 占用 job_store "
            f"单飞槽直到完成。继续? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes"}:
            logger.info("用户取消, 未发起补跑")
            return 0

    from app.jobs import daily_pipeline
    from app.tickflow.policy import detect_capabilities
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore()
    repo = KlineRepository(store)
    capset = detect_capabilities()
    logger.info("capabilities: %d active", len(capset.all()))
    result = daily_pipeline._run_market_daily_scheduled(repo, capset, args.market, full_history=args.full_history)
    status = result.get("status", "unknown")
    logger.info(
        "%s market_daily finished: status=%s completed=%s failed=%s",
        args.market,
        status,
        len(result.get("completed_symbols") or []),
        len(result.get("failed_symbols") or []),
    )
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
