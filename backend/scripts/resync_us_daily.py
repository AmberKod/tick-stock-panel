#!/usr/bin/env python3
"""美股日 K 全量重同步 (把 universe 补到最新交易日)。

背景:
    上次全量同步 (sync_us_daily.py) 停在 2026-09-02, 而 retry_us_daily.py
    只把 60 只失败/class share 标的补到了 2026-09-03。overview 用 max(date)
    作为 as_of, 导致 as_of 被顶到只有 56 只标的的稀疏交易日, 广度/四榜从
    ~6000 只坍缩到 56 只。

    本脚本重新拉取全部 universe 标的的日 K (新浪源返回全历史, merge 时只
    追加新日期, 冗余但安全), 并同步重算 enriched, 使全市场对齐到最新交易日。

用法:
    python backend/scripts/resync_us_daily.py --concurrency 6
    python backend/scripts/resync_us_daily.py --limit 50   # 先小样本验证
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
    fetch_us_daily_akshare,
    sync_hk_daily_to_enriched,
    sync_hk_daily_to_parquet,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="美股日 K 全量重同步到最新交易日")
    parser.add_argument("--concurrency", type=int, default=6, help="并发线程数")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只 (0=全部)")
    parser.add_argument(
        "--skip-enriched", action="store_true",
        help="只写 H6 不算 enriched —— 补数场景统一由 "
             "rebuild_hk_us_enriched.py --target legacy 收口, 避免 compute_enriched "
             "新 76 列/Date schema 与存量 legacy 64 列/Datetime(us) 混存炸全目录 scan",
    )
    args = parser.parse_args()

    instruments = REPO_ROOT / "data" / "instruments" / "us_instruments.parquet"
    if not instruments.exists():
        print(f"[错误] 未找到 {instruments}", file=sys.stderr)
        return 1

    codes = pl.read_parquet(instruments)["code"].to_list()
    if args.limit > 0:
        codes = codes[: args.limit]

    print(f"美股日 K 全量重同步: 共 {len(codes)} 只, 并发 {args.concurrency}", flush=True)

    lock = threading.Lock()
    stats = {"ok": 0, "empty": 0, "err": 0}
    failed: list[str] = []
    t0 = time.time()

    def work(code: str) -> tuple[str, str]:
        try:
            df = fetch_us_daily_akshare(code)
            if df.is_empty():
                return code, "empty"
            sync_hk_daily_to_parquet(df, f"{code}.US")
            if not args.skip_enriched:
                sync_hk_daily_to_enriched(f"{code}.US")
            return code, "ok"
        except Exception as e:
            print(f"异常 {code}: {e}", flush=True)
            return code, "err"

    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(work, c): c for c in codes}
        for fut in as_completed(futures):
            code, status = fut.result()
            with lock:
                stats[status] += 1
                if status != "ok":
                    failed.append(code)
                finished = sum(stats.values())
            if finished % 200 == 0:
                el = time.time() - t0
                rate = finished / el if el > 0 else 0
                print(
                    f"  进度 {finished}/{len(codes)}  成功 {stats['ok']}  空 {stats['empty']} "
                    f"异常 {stats['err']}  ({rate:.2f} 只/s)",
                    flush=True,
                )

    el = time.time() - t0
    print(
        f"完成: 成功 {stats['ok']} 只, 空 {stats['empty']} 只, 异常 {stats['err']} 只, "
        f"耗时 {el:.0f}s ({len(codes)/el if el else 0:.2f} 只/s)",
    )
    if failed:
        print("失败/空清单:", ", ".join(failed[:50]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
