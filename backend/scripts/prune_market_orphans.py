#!/usr/bin/env python
"""港美僵尸标的治理: 把不在当前 universe 的孤儿分区隔离 (quarantine)。

背景
----
美股 H6/enriched 目录有 8806 个 symbol 分区, 而当前 universe (Tushare
NASDAQ 主板列表) 只有 6071 只 —— 多出的 ~2735 个是旧宽 universe 时代的
粉单残留 (权证 W 后缀 / 破产 Q 后缀 / 优先股 P 后缀 / 长期停更僵尸)。
它们从不被增量调度更新, 却拖慢每次 enriched 全目录 scan / matrix 构建
/ rebuild 重算, 且混入选股结果污染数据质量。

行为
----
- 孤儿 (H6 有分区但不在 instruments universe) → 移动到
  data/_quarantine/<时间戳>/<dataset>/symbol=X/ (可整体移回恢复),
  覆盖 kline_daily / kline_hk_us_enriched / adj_factor_hk 三个目录。
- 停更 (在 universe 内但 H6 max_date 落后超阈值) → 仅报告不移动
  (长期停牌可能复牌, 不按数据新旧清理)。
- 默认 dry-run 只出报告; --apply 才真正移动。

用法
----
    python backend/scripts/prune_market_orphans.py --market us          # dry-run
    python backend/scripts/prune_market_orphans.py --market us --apply   # 隔离移动
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import polars as pl

DATA_DIRS = ("kline_daily", "kline_hk_us_enriched", "adj_factor_hk")


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _load_universe(root: Path, market: str) -> set[str]:
    path = root / "instruments" / f"{market.lower()}_instruments.parquet"
    frame = pl.read_parquet(path)
    return set(frame["symbol"].to_list())


def _latest_date(part: Path) -> date | None:
    try:
        latest = pl.scan_parquet(part).select(pl.col("date").max()).collect().item()
    except Exception:
        return None
    return _as_date(latest)


def main() -> int:
    parser = argparse.ArgumentParser(description="港美僵尸标的隔离治理")
    parser.add_argument("--market", choices=["hk", "us", "all"], default="us")
    parser.add_argument("--data-dir", default="data", help="数据根目录 (默认 data)")
    parser.add_argument("--stale-days", type=int, default=180,
                        help="在 universe 内但 H6 停更超过 N 天 → 仅报告 (默认 180)")
    parser.add_argument("--apply", action="store_true", help="真正执行隔离移动 (默认 dry-run)")
    args = parser.parse_args()

    root = _ROOT / args.data_dir if not Path(args.data_dir).is_absolute() else Path(args.data_dir)
    markets = ["hk", "us"] if args.market == "all" else [args.market]
    today = date.today()
    quarantine_root = root / "_quarantine" / datetime.now().strftime("%Y%m%d-%H%M%S")

    total_moved = 0
    for market in markets:
        suffix = ".HK" if market == "hk" else ".US"
        universe = _load_universe(root, market)
        if not universe:
            print(f"[{market.upper()}] universe 为空, 跳过")
            continue

        h6_parts = sorted((root / "kline_daily").glob(f"symbol=*{suffix}"))
        orphans: list[Path] = []
        stale: list[tuple[str, date]] = []
        for symbol_dir in h6_parts:
            symbol = symbol_dir.name.removeprefix("symbol=")
            if symbol not in universe:
                orphans.append(symbol_dir)
                continue
            latest = _latest_date(symbol_dir / "part.parquet")
            if latest is not None and (today - latest).days > args.stale_days:
                stale.append((symbol, latest))

        print(f"\n== {market.upper()} == universe {len(universe)} 只, H6 {len(h6_parts)} 分区")
        print(f"孤儿 (不在 universe, 待隔离): {len(orphans)}")
        if orphans:
            preview = ", ".join(p.name.removeprefix('symbol=') for p in orphans[:10])
            print(f"  样例: {preview}{' ...' if len(orphans) > 10 else ''}")
        print(f"停更 (在 universe, >{args.stale_days} 天无新K, 仅报告): {len(stale)}")
        for symbol, latest in stale[:8]:
            print(f"  停更样例: {symbol} 最新 {latest}")

        if not orphans:
            continue
        if not args.apply:
            size_mb = sum(
                (d / "part.parquet").stat().st_size / 1e6
                for d in orphans if (d / "part.parquet").exists()
            )
            print(f"[dry-run] 将隔离 {len(orphans)} 只 (H6 ~{size_mb:.1f} MB, enriched 同步隔离); 加 --apply 执行")
            continue

        moved = 0
        for symbol_dir in orphans:
            symbol = symbol_dir.name.removeprefix("symbol=")
            for dataset in DATA_DIRS:
                source = root / dataset / f"symbol={symbol}"
                if not source.is_dir():
                    continue
                target = quarantine_root / dataset / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(source), str(target))
                except Exception as exc:
                    print(f"  [WARN] 移动失败 {dataset}/{symbol}: {exc}")
                    continue
            moved += 1
        print(f"[APPLY] 已隔离 {moved}/{len(orphans)} 只 → {quarantine_root}")
        total_moved += moved

    if not args.apply:
        print("\n(dry-run 模式: 未移动任何文件)")
    else:
        print(f"\n完成: 共隔离 {total_moved} 只; 恢复方法 = 把 {quarantine_root} 下对应目录移回 data/<dataset>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
