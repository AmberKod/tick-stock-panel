#!/usr/bin/env python3
"""港股/美股日K → enriched 全量批量计算 (阶段 B 数据层)。

读取 ``data/kline_daily/symbol=*.{HK,US}/part.parquet`` (日K同步已落盘),
逐只调用 ``compute_enriched`` 计算全量指标, 落盘到独立目录
``data/kline_hk_us_enriched/symbol={CODE}.{HK|US}/part.parquet`` (与 A股
kline_daily_enriched 完全隔离, 避免污染 A股 overview 的 as_of 与 board)。

特性:
- 并发计算 (enriched 为纯 CPU 计算, 无网络, 无 py_mini_racer 风险)
- checkpoint 断点续传 + 失败隔离

用法:
    python backend/scripts/sync_hk_us_enriched.py --concurrency 4
    python backend/scripts/sync_hk_us_enriched.py --market HK --limit 50
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.services.hk_data_adapter import (
    _list_market_daily_symbols,
    sync_hk_daily_to_enriched,
)


def _load_progress(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="港美日K → enriched 全量计算")
    parser.add_argument("--concurrency", type=int, default=4, help="并发线程数")
    parser.add_argument("--market", type=str, default="", help="仅处理 HK 或 US (空=全部)")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 只 (0=全部)")
    parser.add_argument(
        "--progress",
        type=str,
        default=str(REPO_ROOT / "data" / "instruments" / "hk_us_enriched_progress.txt"),
    )
    parser.add_argument(
        "--failed",
        type=str,
        default=str(REPO_ROOT / "data" / "instruments" / "hk_us_enriched_failed.txt"),
    )
    args = parser.parse_args()

    symbols = _list_market_daily_symbols(REPO_ROOT / "data")
    if args.market:
        symbols = [s for s in symbols if s.endswith(f".{args.market.upper()}")]
    if args.limit > 0:
        symbols = symbols[: args.limit]

    progress_path = Path(args.progress)
    failed_path = Path(args.failed)
    done = _load_progress(progress_path)
    todo = [s for s in symbols if s not in done]
    print(f"港美 enriched 计算: 总 {len(symbols)} 只, 已完成 {len(done)} 只, 待计算 {len(todo)} 只")

    if not todo:
        print("无待计算标的。")
        return 0

    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0}
    failed: list[str] = []

    def work(sym: str) -> tuple[str, bool]:
        n = sync_hk_daily_to_enriched(sym)
        return sym, n > 0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(work, s): s for s in todo}
        for fut in as_completed(futures):
            sym, ok = fut.result()
            with lock:
                if ok:
                    stats["ok"] += 1
                    with progress_path.open("a", encoding="utf-8") as fh:
                        fh.write(sym + "\n")
                else:
                    stats["fail"] += 1
                    failed.append(sym)
                finished = stats["ok"] + stats["fail"]
            if finished % 100 == 0:
                el = time.time() - t0
                rate = finished / el if el > 0 else 0
                print(
                    f"  进度 {finished}/{len(todo)}  成功 {stats['ok']}  失败 {stats['fail']}  ({rate:.1f} 只/s)",
                    flush=True,
                )

    if failed:
        failed_path.write_text("\n".join(failed) + "\n", encoding="utf-8")
    el = time.time() - t0
    print(
        f"完成: 成功 {stats['ok']} 只, 失败 {stats['fail']} 只, 耗时 {el:.0f}s "
        f"({(stats['ok'] + stats['fail']) / el if el else 0:.1f} 只/s)",
    )
    if failed:
        print(f"失败清单: {failed_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
