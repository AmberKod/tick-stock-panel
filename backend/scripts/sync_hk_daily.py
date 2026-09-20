#!/usr/bin/env python3
"""港股日 K 全量批量同步 (akshare 新浪源)。

数据源: ``ak.stock_hk_daily`` (新浪源; 东财 stock_hk_hist 在当前环境被
代理拦截 ProxyError, 新浪源可用)。

特性 (与 sync_us_daily.py 对齐):
- 并发拉取 (默认 8 线程), 失败跳过
- checkpoint 断点续传: 每成功落盘一只即记录到 progress 文件, 中断后可续跑
- 失败隔离: 拉取失败的 ticker 记录到 failed 文件, 不阻塞其余标的
- 落盘复用 ``sync_hk_daily_to_parquet``, 分区 symbol={code}.HK

用法:
    python backend/scripts/sync_hk_daily.py --concurrency 8
    python backend/scripts/sync_hk_daily.py --limit 50   # 先小样本验证链路
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

import polars as pl

from app.services.hk_data_adapter import (
    fetch_hk_daily_sina,
    sync_hk_daily_to_parquet,
)


def _load_progress(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description="港股日 K 全量批量同步")
    parser.add_argument("--concurrency", type=int, default=8, help="并发线程数")
    parser.add_argument("--limit", type=int, default=0, help="最多同步 N 只 (0=全部)")
    parser.add_argument(
        "--progress",
        type=str,
        default=str(REPO_ROOT / "data" / "instruments" / "hk_daily_progress.txt"),
    )
    parser.add_argument(
        "--failed",
        type=str,
        default=str(REPO_ROOT / "data" / "instruments" / "hk_daily_failed.txt"),
    )
    args = parser.parse_args()

    instruments = REPO_ROOT / "data" / "instruments" / "hk_instruments.parquet"
    if not instruments.exists():
        print(f"[错误] 未找到 {instruments}, 请先运行 sync_hk_instruments", file=sys.stderr)
        return 1

    codes = pl.read_parquet(instruments)["code"].to_list()
    if args.limit > 0:
        codes = codes[: args.limit]

    progress_path = Path(args.progress)
    failed_path = Path(args.failed)
    done = _load_progress(progress_path)
    todo = [c for c in codes if c not in done]
    print(f"港股日 K 同步: 总 {len(codes)} 只, 已完成 {len(done)} 只, 待同步 {len(todo)} 只")

    if not todo:
        print("无待同步标的。")
        return 0

    lock = threading.Lock()
    stats = {"ok": 0, "fail": 0}
    failed: list[str] = []

    def work(code: str) -> tuple[str, bool]:
        df = fetch_hk_daily_sina(code)
        if df.is_empty():
            return code, False
        sync_hk_daily_to_parquet(df, f"{code}.HK")
        return code, True

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futures):
            code, ok = fut.result()
            with lock:
                if ok:
                    stats["ok"] += 1
                    with progress_path.open("a", encoding="utf-8") as fh:
                        fh.write(code + "\n")
                else:
                    stats["fail"] += 1
                    failed.append(code)
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
