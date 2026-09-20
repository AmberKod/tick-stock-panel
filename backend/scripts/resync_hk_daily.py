#!/usr/bin/env python3
"""港股日 K 全量重同步 (把 universe 补到最新交易日)。

背景:
    上次全量同步 (sync_hk_daily.py) 停在 2026-09-03。本批次因
    run_market_daily_sync 框架依赖腾讯日历(2026-09-14 起 501 不可用),
    改走「单只全量拉取 + 直接写 H6 + 同步 enriched」绕开 framework,
    对应 09-04~09-11 增量(共 6 个交易日)。

    港股新浪源 fetch_hk_daily_sina 一次拉全历史, 内部 OHLC 校验偶发失败
    (返回空 df); 失败重试即可, 同 retry_us_daily.py 模式。

用法:
    python backend/scripts/resync_hk_daily.py --concurrency 6
    python backend/scripts/resync_hk_daily.py --limit 20 --concurrency 4
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import polars as pl

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.hk_data_adapter import (
    fetch_hk_daily_sina,
    sync_hk_daily_to_enriched,
    sync_hk_daily_to_parquet,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="港股日 K 全量重同步到最新交易日")
    parser.add_argument("--concurrency", type=int, default=6, help="并发线程数")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只 (0=全部)")
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="单只最大重试次数",
    )
    parser.add_argument(
        "--skip-enriched", action="store_true",
        help="只写 H6 不算 enriched —— 补数场景统一由 "
             "rebuild_hk_us_enriched.py --target legacy 收口, 避免 compute_enriched "
             "新 76 列/Date schema 与存量 legacy 64 列/Datetime(us) 混存炸全目录 scan",
    )
    parser.add_argument(
        "--start", type=int, default=0, help="从 universe 第 N 只开始 (用于续跑)",
    )
    args = parser.parse_args()

    instruments = REPO_ROOT / "data" / "instruments" / "hk_instruments.parquet"
    if not instruments.exists():
        print(f"[错误] 未找到 {instruments}", file=sys.stderr)
        return 1

    inst = pl.read_parquet(instruments)
    codes = inst["code"].to_list() if "code" in inst.columns else inst["symbol"].to_list()
    if args.start > 0:
        codes = codes[args.start:]
    if args.limit > 0:
        codes = codes[: args.limit]

    print(
        f"港股日 K 全量重同步: 共 {len(codes)} 只, 并发 {args.concurrency}, "
        f"重试 {args.max_attempts}",
        flush=True,
    )

    lock = threading.Lock()
    stats = {"ok": 0, "empty": 0, "err": 0}
    failed: list[str] = []
    t0 = time.time()

    def work(code: str) -> tuple[str, str]:
        last_err = ""
        for attempt in range(1, args.max_attempts + 1):
            try:
                df = fetch_hk_daily_sina(code)
                if df.is_empty():
                    last_err = "empty"
                    if attempt < args.max_attempts:
                        time.sleep(0.5 * attempt)
                        continue
                    return code, "empty"
                # 写 H6 (kline_daily/symbol=X.HK/part.parquet); enriched 仅在
                # 非补数场景按需触发 (补数统一走 rebuild --target legacy)
                sync_hk_daily_to_parquet(df, f"{code}.HK")
                if not args.skip_enriched:
                    sync_hk_daily_to_enriched(f"{code}.HK")
                return code, "ok"
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < args.max_attempts:
                    time.sleep(0.5 * attempt)
                    continue
                return code, "err"
        return code, "empty" if last_err == "empty" else "err"

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(work, c): c for c in codes}
        for fut in as_completed(futures):
            code, status = fut.result()
            with lock:
                stats[status] += 1
                if status != "ok":
                    failed.append(code)
                finished = sum(stats.values())
            if finished % 100 == 0 or finished == len(codes):
                el = time.time() - t0
                rate = finished / el if el > 0 else 0
                print(
                    f"  进度 {finished}/{len(codes)}  成功 {stats['ok']}  空 {stats['empty']} "
                    f"异常 {stats['err']}  ({rate:.2f} 只/s)",
                    flush=True,
                )

    el = time.time() - t0
    rate = (len(codes) / el) if el else 0
    print(
        f"完成: 成功 {stats['ok']} 只, 空 {stats['empty']} 只, 异常 {stats['err']} 只, "
        f"耗时 {el:.0f}s ({rate:.2f} 只/s)",
    )
    if failed:
        print(f"失败/空清单(前 50): {', '.join(failed[:50])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())