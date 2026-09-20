#!/usr/bin/env python
"""港美股 enriched 全量重算脚本 (H6 kline_daily -> kline_hk_us_enriched).

背景
----
P1-A (commit f6f4a9d) 发现港美 enriched 停在 2026-09-03, 而 H6 (kline_daily) 已到
2026-09-11 —— enriched 只是没被重算, 不是数据源缺。

为什么必须"全量重算"而不是"增量补"
-----------------------------------
enriched 分区目录是 per-symbol 单文件, 但 A 股/hk/us 三份会一起被 pl.scan_parquet
扫 (详见 MEMORY.md 的 "polars scan_parquet 跨分区 schema 必须一致")。历史 enriched
是旧 compute_enriched 产出的 64 列, 当前代码产出 76 列。若只补部分 symbol, 新旧
schema 混存会让 scan 报列名冲突, 连带 regime / matrix / overview 全线失败。
所以本脚本默认对所有 symbol 重算, 保证全目录 schema 一致。

市场差异
--------
- us:   _compute_market_enriched 走 compute_enriched(factors=None), 无外部依赖, 可直算。
- hk:   _compute_market_enriched 会校验 adj_factor_hk/symbol=XXX/part.parquet
        (_hk_factors_for_window), 缺失会 raise "港股复权因子缺失"。akshare 无港股复权
        因子接口, 需先用 --bootstrap-hk-factors 由 stock_hk_hist 前复权价反推。

用法
----
    # 美股全量重算 (推荐先 --limit 50 冒烟)
    python backend/scripts/rebuild_hk_us_enriched.py --market us --limit 50
    python backend/scripts/rebuild_hk_us_enriched.py --market us --concurrency 8

    # 先看有多少只待重算 + 港股复权因子覆盖度 (不写盘)
    python backend/scripts/rebuild_hk_us_enriched.py --dry-run

幂等: 重复执行结果一致; 每只独立写盘, 中断后重跑不会丢已完成部分。
不主动后台化: 脚本设计为前台运行 + 打印进度, 长跑由使用者自行 nohup/终端保持。
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 让脚本能直接 python 运行 (不必 PYTHONPATH=backend)
_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


from app.services.hk_data_adapter import _compute_market_enriched
from app.tickflow.market_daily import (
    HK_US_ENRICHED_DIR as ENRICHED_DIR,
)
from app.tickflow.market_daily import (
    downgrade_enriched,
    legacy_enriched_columns,
    read_market_daily_symbol,
)


def _iter_symbols(root: Path, market: str) -> list[str]:
    """列出该市场在 H6 层存在的所有 symbol (源文件 dir layout: symbol=XXX.US/)."""
    suffix = ".HK" if market == "hk" else ".US"
    out: list[str] = []
    for d in (root / "kline_daily").glob(f"symbol=*{suffix}"):
        out.append(d.name.replace("symbol=", ""))
    return sorted(out)


def _factor_status(root: Path, symbol: str) -> bool:
    """港股复权因子是否就位。"""
    return (root / "adj_factor_hk" / f"symbol={symbol}" / "part.parquet").exists()


def _legacy_schema(root: Path) -> list[str] | None:
    """兼容基线改为共享实现 (app.tickflow.market_daily), 保留旧名供本脚本调用。"""
    return legacy_enriched_columns(root)


def rebuild_one(root: Path, symbol: str, target: str = "new") -> dict:
    """重算单只 symbol 的 enriched 并写盘。返回状态 dict (不抛异常)。

    target="new"   : 输出当前 compute_enriched 的完整 schema (76 列, date=Date)。
                     必须港美同时全量重算, 否则与存量混存会导致 scan_parquet 失败。
    target="legacy": 降级输出旧 64 列 + date=Datetime('us'), 与存量目录兼容,
                     可安全增量补跑。新产出的 12 个价格身份列会被裁掉
                     (adjustment_* / price_* / source / observed_at 等)。
    """
    result = {"symbol": symbol, "status": "ok", "rows": 0, "error": None}
    try:
        target_path = root / ENRICHED_DIR / f"symbol={symbol}" / "part.parquet"
        # legacy 降级基线必须在覆盖前取, 否则该文件本身若已是旧 schema 会失效
        legacy_cols = _legacy_schema(root) if target == "legacy" else None
        if target == "legacy" and legacy_cols is None:
            result["status"] = "failed"
            result["error"] = "取不到旧 schema 基线, legacy 模式不可用"
            return result

        raw = read_market_daily_symbol(root, symbol)
        if raw.is_empty():
            result["status"] = "skipped_empty_raw"
            return result
        enriched = _compute_market_enriched(root, symbol, raw)
        if enriched.is_empty():
            result["status"] = "skipped_empty_enriched"
            return result

        if target == "legacy":
            try:
                enriched = downgrade_enriched(enriched, legacy_cols)
            except ValueError as exc:
                result["status"] = "failed"
                result["error"] = str(exc)
                return result

        target_path.parent.mkdir(parents=True, exist_ok=True)
        enriched.write_parquet(target_path)
        result["rows"] = enriched.height
        result["latest"] = str(enriched["date"].max())
    except Exception as e:
        result["status"] = "failed"
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="港美股 enriched 全量重算")
    ap.add_argument("--data-dir", default="data", help="数据根目录 (默认 data)")
    ap.add_argument("--market", choices=["us", "hk", "all"], default="us")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只 (0=全部)")
    ap.add_argument("--start", type=int, default=0, help="从第 N 只开始 (配合中断续跑)")
    ap.add_argument("--concurrency", type=int, default=6, help="并发线程数")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    ap.add_argument(
        "--target",
        choices=["new", "legacy"],
        default="legacy",
        help="输出 schema: legacy=兼容存量 64 列(默认, 可增量补跑); "
        "new=76 列新 schema(必须港美同时全量重算)",
    )
    args = ap.parse_args()

    root = Path(args.data_dir).resolve()
    if not (root / "kline_daily").exists():
        print(f"[FATAL] 找不到 {root / 'kline_daily'}", file=sys.stderr)
        return 2

    markets = ["us", "hk"] if args.market == "all" else [args.market]

    total_plan = 0
    for market in markets:
        symbols = _iter_symbols(root, market)
        if args.start:
            symbols = symbols[args.start:]
        if args.limit:
            symbols = symbols[: args.limit]

        if market == "hk":
            have = sum(1 for s in symbols if _factor_status(root, s))
            print(
                f"[PLAN] {market}: {len(symbols)} 只待重算, "
                f"其中 {have} 只有复权因子 / {len(symbols) - have} 只缺 (会被跳过)",
            )
        else:
            print(f"[PLAN] {market}: {len(symbols)} 只待重算")
        print(f"       target={args.target}, concurrency={args.concurrency}")
        total_plan += len(symbols)

        if args.dry_run:
            continue

        t0 = time.time()
        counters: dict[str, int] = {}
        failures: list[dict] = []
        done = 0

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(rebuild_one, root, s, args.target): s for s in symbols}
            for fut in as_completed(futures):
                res = fut.result()
                counters[res["status"]] = counters.get(res["status"], 0) + 1
                if res["status"] == "failed" and len(failures) < 20:
                    failures.append(res)
                done += 1
                if done % 200 == 0 or done == len(symbols):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed else 0
                    print(
                        f"  [{market}] {done}/{len(symbols)} "
                        f"({done / len(symbols) * 100:.1f}%) "
                        f"{rate:.1f} 只/秒 err={counters.get('failed', 0)}",
                        flush=True,
                    )

        elapsed = time.time() - t0
        print(
            f"[DONE] {market}: {elapsed:.1f}s, ok={counters.get('ok', 0)}, "
            f"failed={counters.get('failed', 0)}, skipped="
            f"{counters.get('skipped_empty_raw', 0) + counters.get('skipped_empty_enriched', 0)}",
        )
        if failures:
            print("  失败样例 (最多 20):")
            for f in failures[:20]:
                print(f"    {f['symbol']}: {f['error']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
